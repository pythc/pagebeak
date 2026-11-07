"""Narrow, expiring transfers for Skill scripts running inside the teacher's WorkBuddy.

No endpoint accepts a local path. A grant can upload only one hash-bound file or
one device inventory. It cannot read documents or act as a general API token.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from akb import local_catalog, services, space
from akb.models import Credential, HostTransfer, User, now
from akb.schemas import Input, UploadInput
from akb.security import authenticate, digest_token, require


class HostUpload(Input):
    request_id: UUID
    device_id: UUID
    document_id: UUID
    file: UploadInput


class HostSync(Input):
    request_id: UUID
    device_id: UUID
    size: int = Field(ge=1, le=16 * 1024**2)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class SyncPayload(Input):
    catalog: local_catalog.CatalogSnapshot | None = None
    observations: local_catalog.Observations


def prepare(session, settings, p, body, kind):
    require(p, "knowledge:read")
    require(p, "knowledge:write")
    require(p, "profile:read")
    services.lock_tenant(session, p.tenant_id)
    spec = body.model_dump(mode="json")
    if kind == "upload" and not body.file.sha256:
        raise HTTPException(422, "本机收录必须绑定原文件 SHA-256")
    previous = session.scalar(select(HostTransfer).where(HostTransfer.request_id == str(body.request_id)))
    if previous and (
        previous.kind != kind or previous.spec != spec or previous.credential_id != p.credential_id
    ):
        raise HTTPException(409, "同一传输请求的文件、设备或身份已变化")
    if previous is None:
        previous = HostTransfer(
            tenant_id=p.tenant_id,
            request_id=str(body.request_id),
            kind=kind,
            credential_id=p.credential_id,
            spec=spec,
            result=None,
        )
        if kind == "upload":
            previous.upload_id = space.begin_upload(
                session, settings, p.tenant_id, body.file, "host-" + str(body.request_id)
            ).id
        session.add(previous)
    token = "zxt_" + secrets.token_urlsafe(32)
    previous.digest, previous.expires_at = digest_token(token), now() + timedelta(minutes=20)
    session.flush()
    origin = settings.assistant_public_origin or f"http://127.0.0.1:{settings.api_port}"
    services.audit(session, p.tenant_id, "host_transfer.prepare", previous.id)
    return {
        "contract": "zhixu-host-transfer-v1",
        "knowledge_base_id": p.tenant_id,
        "device_id": str(body.device_id),
        "kind": kind,
        "url": f"{origin}/v2/host-transfers/{p.tenant_id}/{previous.id}",
        "token": token,
        "expires_at": previous.expires_at,
        "spec": spec,
        "chunk_bytes": settings.upload_chunk_bytes,
        "notice": "仅本次指定文件/清单的短期凭据；保存到私有临时 JSON，勿展示或写入命令参数/日志。不是账号令牌。",
    }


def grant(session, tenant, grant_id, authorization, kind=None):
    if not authorization.startswith("Bearer zxt_") or len(authorization) > 200:
        raise HTTPException(401, "传输凭据无效")
    row = session.get(HostTransfer, str(grant_id))
    if not row or not hmac.compare_digest(row.digest, digest_token(authorization[7:])):
        raise HTTPException(401, "传输凭据无效")
    credential = session.get(Credential, row.credential_id)
    user = session.get(User, credential.user_id) if credential else None
    if (
        row.expires_at <= now()
        or not credential
        or credential.revoked
        or credential.expires_at <= now()
        or not user
        or user.tenant_id != tenant
        or not {"knowledge:read", "knowledge:write", "profile:read"} <= set(credential.scopes)
    ):
        raise HTTPException(401, "传输或原连接授权已过期/撤销，请通过原 MCP 重新准备")
    if kind and row.kind != kind:
        raise HTTPException(403, "传输用途不匹配")
    return row


def routes(app, db, settings):
    @app.post("/v2/host-transfers/upload")
    def upload_ticket(body: HostUpload, p=Depends(authenticate)):
        with db.session(p.tenant_id, actor=p.kind) as session:
            return prepare(session, settings, p, body, "upload")

    @app.post("/v2/host-transfers/catalog")
    def sync_ticket(body: HostSync, p=Depends(authenticate)):
        with db.session(p.tenant_id, actor=p.kind) as session:
            return prepare(session, settings, p, body, "catalog")

    @app.get("/v2/host-transfers/{tenant}/{grant_id}")
    def status(tenant: UUID, grant_id: UUID, authorization: str = Header(default="")):
        with db.session(str(tenant)) as session:
            row = grant(session, str(tenant), grant_id, authorization)
            return {
                "kind": row.kind,
                "result": row.result,
                "upload": space.upload_dict(space.get_upload(session, row.upload_id))
                if row.upload_id
                else None,
            }

    @app.put("/v2/host-transfers/{tenant}/{grant_id}")
    async def content(
        tenant: UUID,
        grant_id: UUID,
        request: Request,
        offset: int = Query(0, ge=0),
        authorization: str = Header(default=""),
    ):
        with db.session(str(tenant)) as session:
            row = grant(session, str(tenant), grant_id, authorization)
            limit = settings.upload_chunk_bytes if row.kind == "upload" else row.spec["size"]
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                raise HTTPException(413, "传输大小超过本次许可")

        def persist():
            with db.session(str(tenant), actor="api") as session:
                services.lock_tenant(session, str(tenant))
                row = grant(session, str(tenant), grant_id, authorization)
                if row.kind == "upload":
                    return space.append_chunk(
                        session, settings, str(tenant), row.upload_id, offset, bytes(data)
                    )
                if len(data) != row.spec["size"] or hashlib.sha256(data).hexdigest() != row.spec["sha256"]:
                    raise HTTPException(422, "清单大小或 SHA-256 与许可不符")
                if row.result is not None:
                    return row.result
                try:
                    body = SyncPayload.model_validate_json(data)
                except (ValueError, TypeError):
                    raise HTTPException(422, "清单格式不合法") from None
                if (
                    str(body.observations.device_id) != row.spec["device_id"]
                    or body.catalog
                    and str(body.catalog.device_id) != row.spec["device_id"]
                ):
                    raise HTTPException(403, "清单设备与许可不符")
                # Validate both in the same transaction; rollback on revision/ownership conflicts.
                observed = local_catalog.observe(session, str(tenant), body.observations)
                catalog = local_catalog.publish(session, str(tenant), body.catalog) if body.catalog else None
                row.result = {"observations": observed, "catalog": catalog}
                return row.result

        return await run_in_threadpool(persist)

    @app.post("/v2/host-transfers/{tenant}/{grant_id}/complete")
    def complete(tenant: UUID, grant_id: UUID, authorization: str = Header(default="")):
        with db.session(str(tenant), actor="api") as session:
            services.lock_tenant(session, str(tenant))
            row = grant(session, str(tenant), grant_id, authorization, "upload")
            if row.result is not None:
                return row.result
            result = space.finish_upload(session, settings, str(tenant), row.upload_id)
            source = result["source"]
            observation = local_catalog.Observations.model_validate(
                {
                    "device_id": row.spec["device_id"],
                    "items": [
                        {
                            "document_id": row.spec["document_id"],
                            "source_id": source["id"],
                            "sha256": source["sha256"],
                            "current_hash": source["sha256"],
                            "status": "indexed",
                        }
                    ],
                }
            )
            local_catalog.observe(session, str(tenant), observation)
            row.result = jsonable_encoder(result)
            path = space.staging_path(settings, space.get_upload(session, row.upload_id))
        path.unlink(missing_ok=True)
        return result

    @app.delete("/v2/host-transfers/{tenant}/{grant_id}")
    def cancel(tenant: UUID, grant_id: UUID, authorization: str = Header(default="")):
        with db.session(str(tenant), actor="api") as session:
            row = grant(session, str(tenant), grant_id, authorization, "upload")
            return space.cancel_upload(session, settings, str(tenant), row.upload_id)
