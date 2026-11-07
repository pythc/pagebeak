import secrets
from datetime import timedelta
from typing import Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID

import httpx
from fastapi import Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, text

from akb import changes, native, organization, services
from akb.models import (
    Credential,
    Evidence,
    FileChange,
    Idempotency,
    Job,
    NativeConnection,
    NativeLinkCode,
    Source,
    now,
)
from akb.security import Principal, authenticate, browser_only, digest_token, require


class ConnectInput(BaseModel):
    code: str = Field(min_length=32, max_length=200)
    access_token: str = Field(min_length=20, max_length=10000)
    password: str = Field(min_length=1, max_length=1000)


class AcknowledgeInput(BaseModel):
    expected_hash: str = Field(pattern="^[a-f0-9]{64}$")
    through_change_id: int = Field(ge=0)


class NativeNotice(BaseModel):
    request_id: UUID
    operation: Literal[
        "create", "upload", "rename", "move", "restore", "delete", "content_update", "metadata"
    ]
    targets: list[str] = Field(default_factory=list, max_length=100)
    new_name: str = Field(default="", max_length=255)


def tasks(session):
    names = {s.id: s.filename for s in session.scalars(select(Source))}
    return [
        {
            "id": j.id,
            "kind": j.kind,
            "status": j.status,
            "source_id": j.source_id,
            "filename": names.get(j.source_id),
            "attempts": j.attempts,
            "created_at": j.created_at,
            "result": j.result,
            "error": j.error,
        }
        for j in session.scalars(
            select(Job)
            .where(
                or_(
                    Job.kind != "native_sync",
                    Job.status != "completed",
                    Job.result["changed"].as_integer().is_(None),
                    Job.result["changed"].as_integer() > 0,
                )
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(200)
        )
    ]


def history(session, after=0, limit=100):
    rows = list(
        session.scalars(
            select(FileChange).where(FileChange.id > after).order_by(FileChange.id).limit(limit + 1)
        )
    )
    items = rows[:limit]
    return {
        "items": [
            {
                "id": c.id,
                "file_id": c.file_id,
                "source_id": c.source_id,
                "action": c.action,
                "actor": c.actor,
                "before": c.before,
                "after": c.after,
                "created_at": c.created_at,
            }
            for c in items
        ],
        "next_cursor": items[-1].id if items else after,
        "has_more": len(rows) > limit,
    }


def routes(app, db, settings):
    @app.post("/v1/native/link-code")
    def link_code(p: Principal = Depends(authenticate)):
        browser_only(p)
        code = secrets.token_urlsafe(32)
        with db.session(p.tenant_id) as session:
            session.add(
                NativeLinkCode(
                    tenant_id=p.tenant_id, digest=digest_token(code), expires_at=now() + timedelta(minutes=5)
                )
            )
        return {"code": code, "expires_in": 300}

    @app.post("/v1/native/connect")
    def connect(body: ConnectInput):
        identity = native.verify_identity(settings, body.access_token)
        with db.session() as session:
            tenant_id = session.scalar(
                text("SELECT public.akb_native_code_tenant(:digest)"), {"digest": digest_token(body.code)}
            )
        if not tenant_id:
            raise HTTPException(403, "连接码无效、过期或已使用，请从知识库重新进入")
        # A separate session avoids rotating the native browser's refresh token out from under it.
        with httpx.Client(base_url=settings.native_cloudreve_url, trust_env=False, timeout=20) as client:
            response = client.post(
                "/api/v4/session/token", json={"email": identity["email"], "password": body.password}
            ).json()
        if response.get("code") != 0 or response.get("data", {}).get("user", {}).get("id") != identity["id"]:
            raise HTTPException(401, "文件空间密码不正确，或该账号需要尚未支持的二次验证")
        with db.session(tenant_id) as session:
            services.lock_tenant(session, tenant_id)
            code = session.scalar(
                select(NativeLinkCode)
                .where(NativeLinkCode.digest == digest_token(body.code))
                .with_for_update()
            )
            if not code or code.consumed or code.expires_at <= now():
                raise HTTPException(403, "连接码已失效")
            connection = session.scalar(select(NativeConnection))
            if connection and connection.provider_user_id != identity["id"]:
                raise HTTPException(409, "此知识库已连接另一个文件空间，不能直接覆盖")
            if not connection:
                connection = NativeConnection(tenant_id=tenant_id, provider_user_id=identity["id"])
                session.add(connection)
                session.flush()
            native.save_token(settings, connection.id, response["data"]["token"])
            code.consumed = True
            connection.enabled, connection.error = True, None
            native.enqueue(session, tenant_id, "frontend_observed")
        return {"connected": True}

    def native_tenant(x_cloudreve_token: str = Header(min_length=20, max_length=10000)):
        identity = native.verify_identity(settings, x_cloudreve_token)
        with db.session() as session:
            tenant = session.scalar(text("SELECT public.akb_native_tenant(:id)"), {"id": identity["id"]})
        if not tenant:
            raise HTTPException(409, "请先从知识库的“我的文件”入口连接当前账号")
        return tenant

    @app.get("/v1/native/board")
    def board(tenant_id=Depends(native_tenant)):
        from akb.knowledge import tasks as knowledge_tasks

        with db.session(tenant_id) as session:
            connection = session.scalar(select(NativeConnection))
            recent = list(session.scalars(select(FileChange).order_by(FileChange.id.desc()).limit(30)))
            return {
                "tasks": tasks(session),
                "sync": changes.status(session),
                "knowledge": knowledge_tasks(session, limit=5),
                "last_scan_at": connection.last_scan_at,
                "history": [
                    {
                        "id": c.id,
                        "action": c.action,
                        "actor": c.actor,
                        "before": c.before,
                        "after": c.after,
                        "created_at": c.created_at,
                    }
                    for c in recent
                ],
            }

    @app.post("/v1/native/wake", status_code=202)
    def wake(tenant_id=Depends(native_tenant)):
        with db.session(tenant_id) as session:
            job = native.enqueue(session, tenant_id, "frontend_observed")
            return {"job_id": job.id}

    @app.post("/v1/native/notice", status_code=202)
    def notice(body: NativeNotice, tenant_id=Depends(native_tenant)):
        if any(
            urlsplit(path).scheme != "cloudreve" or urlsplit(path).netloc != "my" or len(path) > 2048
            for path in body.targets
        ):
            raise HTTPException(422, "只记录当前个人空间的操作")
        payload = body.model_dump(mode="json")
        fingerprint = changes.digest(payload)
        with db.session(tenant_id) as session:
            services.lock_tenant(session, tenant_id)
            existing = session.scalar(
                select(Idempotency).where(
                    Idempotency.operation == "native-notice", Idempotency.key == str(body.request_id)
                )
            )
            if existing:
                if existing.request_hash != fingerprint:
                    raise HTTPException(409, "相同操作标识对应了不同的记录")
                return {"recorded": True, "replayed": True}
            # Reports retain rapid A→B→A operations; verified hashes still come from the server scan.
            changes.record(
                session,
                tenant_id,
                "notice:" + str(body.request_id),
                "frontend_operation",
                "frontend_reported",
                {},
                {
                    **payload,
                    "name": body.new_name
                    or (
                        unquote(urlsplit(body.targets[0]).path).rsplit("/", 1)[-1]
                        if body.targets
                        else "文件操作"
                    ),
                },
            )
            session.add(
                Idempotency(
                    tenant_id=tenant_id,
                    operation="native-notice",
                    key=str(body.request_id),
                    request_hash=fingerprint,
                    resource_id=str(body.request_id),
                )
            )
            job = native.enqueue(session, tenant_id, "frontend_observed")
            return {"recorded": True, "replayed": False, "job_id": job.id}

    @app.get("/v1/sync/state")
    def sync_state(p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id) as session:
            result = changes.status(session)
            credential = session.get(Credential, p.credential_id)
            return {
                **result,
                "agent_observed_hash": credential.observed_hash,
                "agent_change_cursor": credential.change_cursor,
                "agent_needs_review": result["latest_change_id"] > credential.change_cursor
                or result["current_hash"] != credential.observed_hash,
            }

    @app.post("/v1/changes/acknowledge")
    def acknowledge(body: AcknowledgeInput, p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id) as session:
            services.lock_tenant(session, p.tenant_id)
            credential = session.scalar(
                select(Credential).where(Credential.id == p.credential_id).with_for_update()
            )
            result = changes.status(session)
            if (
                body.expected_hash != result["current_hash"]
                or body.through_change_id != result["latest_change_id"]
                or result["needs_update"]
                or result["scan_error"]
                or result["scan_pending"]
            ):
                raise HTTPException(409, "文件或任务状态已变化，请重新核对后确认；检查点未推进")
            if (
                body.through_change_id == credential.change_cursor
                and body.expected_hash == credential.observed_hash
            ):
                return {"acknowledged": True, "through_change_id": credential.change_cursor, "replayed": True}
            credential.change_cursor, credential.observed_hash = body.through_change_id, body.expected_hash
            return {"acknowledged": True, "through_change_id": credential.change_cursor, "replayed": False}

    @app.get("/v1/changes")
    def read_changes(
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
        p: Principal = Depends(authenticate),
    ):
        require(p, "knowledge:read")
        with db.session(p.tenant_id) as session:
            return history(session, after, limit)

    @app.get("/v1/tasks")
    def read_tasks(p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id) as session:
            return {"items": tasks(session)}

    @app.get("/v1/changes/{event_id}/content")
    def change_content(
        event_id: int,
        version: str = Query("before", pattern="^(before|after)$"),
        offset: int = Query(0, ge=0),
        limit: int = Query(10, ge=1, le=30),
        p: Principal = Depends(authenticate),
    ):
        require(p, "knowledge:read")
        with db.session(p.tenant_id) as session:
            event = session.get(FileChange, event_id)
            if not event:
                raise HTTPException(404, "变更记录不存在")
            source = session.get(Source, event.source_id) if event.source_id else None
            snapshot = event.before if version == "before" else event.after
            generation = snapshot.get("generation_id")
            if not source or source.revoked_at or not generation:
                return {
                    "items": [],
                    "content_available": False,
                    "has_more": False,
                    "reason": "资料已撤回，或该版本没有完成解析",
                }
            rows = list(
                session.scalars(
                    select(Evidence)
                    .where(Evidence.source_id == source.id, Evidence.generation_id == generation)
                    .order_by(Evidence.ordinal)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return {
                "items": [{"id": e.id, "text": e.text, "locator": e.locator} for e in rows[:limit]],
                "version": version,
                "generation_id": generation,
                "content_available": bool(rows),
                "historical": generation != source.generation_id,
                "offset": offset,
                "has_more": len(rows) > limit,
            }

    @app.post("/v1/sync/reconcile", status_code=202)
    def reconcile(p: Principal = Depends(authenticate)):
        require(p, "knowledge:write")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            connection = session.scalar(select(NativeConnection).where(NativeConnection.enabled.is_(True)))
            jobs = [native.enqueue(session, p.tenant_id, "external").id] if connection else []
            job = organization.enqueue(session, p.tenant_id)
            if job:
                jobs.append(job.id)
            return {"job_ids": jobs, "status": "queued"}
