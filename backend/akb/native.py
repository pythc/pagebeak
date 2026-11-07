"""Observe a user's native drive through a separate server session; never use the storage broker identity."""

import fcntl
import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from akb import changes, organization, services
from akb.db import Database
from akb.models import Claim, Job, NativeConnection, NativeFile, Source, now, uid


def token_path(settings, connection_id):
    return settings.native_secrets_dir / (str(UUID(connection_id)) + ".json")


def save_token(settings, connection_id, token):
    target = token_path(settings, connection_id)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as stream:
        json.dump(token, stream)
        stream.flush()
        os.fsync(stream.fileno())
        stage = stream.name
    os.replace(stage, target)


def verify_identity(settings, token):
    try:
        with httpx.Client(base_url=settings.native_cloudreve_url, timeout=15, trust_env=False) as client:
            result = client.get("/api/v4/user/me", headers={"Authorization": "Bearer " + token}).json()
        if result.get("code") != 0 or not result.get("data", {}).get("id"):
            raise HTTPException(401, "文件空间登录已失效，请重新登录")
        return result["data"]
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "文件服务暂不可达") from exc


class NativeClient:
    def __init__(self, settings, connection_id, tenant_id=None):
        self.settings, self.connection_id = settings, connection_id
        self.tenant_id = tenant_id
        self.client = httpx.Client(base_url=settings.native_cloudreve_url, timeout=45, trust_env=False)

    def close(self):
        self.client.close()

    def access_token(self):
        target = token_path(self.settings, self.connection_id)
        with target.with_suffix(".lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            token = json.loads(target.read_text())
            if datetime.fromisoformat(token["access_expires"].replace("Z", "+00:00")) <= now() + timedelta(
                seconds=30
            ):
                result = self.client.post(
                    "/api/v4/session/token/refresh", json={"refresh_token": token["refresh_token"]}
                ).json()
                if result.get("code") != 0:
                    # Auto-created spaces have a server-side credential. Refresh
                    # only this exact, enabled binding; never re-provision/reset it.
                    token = self.renew_managed_session()
                else:
                    token = result["data"]
                save_token(self.settings, self.connection_id, token)
            return token["access_token"]

    def renew_managed_session(self):
        if self.settings.passwordless_enabled and self.tenant_id:
            from akb.guest_space import managed_login, managed_path
            path = managed_path(self.settings, self.tenant_id)
            if path.exists() and not path.is_symlink():
                db = Database(self.settings.database_url)
                try:
                    with db.session(self.tenant_id) as session:
                        connection = session.get(NativeConnection, self.connection_id)
                        identity = json.loads(path.read_text())
                        if (connection and connection.enabled and connection.tenant_id == self.tenant_id
                                and identity.get('origin') == self.settings.native_cloudreve_url
                                and identity.get('provider_id') == connection.provider_user_id):
                            return managed_login(self.settings, identity)['token']
                finally:
                    db.close()
        raise OSError("文件空间连接已过期，请重新连接")

    def call(self, method, path, **kwargs):
        result = self.client.request(
            method, "/api/v4/" + path, headers={"Authorization": "Bearer " + self.access_token()}, **kwargs
        )
        result.raise_for_status()
        value = result.json()
        if value.get("code") != 0:
            raise OSError(f"文件空间操作失败（代码 {value.get('code')}）")
        return value["data"]

    def inventory(self, heartbeat=lambda **_: None):
        queue, seen, files = deque(["cloudreve://my"]), set(), {}
        while queue:
            path = queue.popleft()
            if path in seen:
                raise OSError("文件夹遍历出现重复路径")
            seen.add(path)
            pagination, requested = {"page": 0}, set()
            while True:
                key = changes.digest(pagination)
                if key in requested:
                    raise OSError("文件目录分页重复，未执行删除对账")
                requested.add(key)
                result = self.call("GET", "file", params={"uri": path, "page_size": 200, **pagination})
                if result.get("recursion_limit_reached"):
                    raise OSError("目录未完整返回，未执行删除对账")
                for file in result["files"]:
                    if file.get("metadata", {}).get("sys:upload_session_id"):
                        raise OSError("有文件正在上传，将稍后重新对账")
                    files[file["id"]] = {
                        k: file.get(k)
                        for k in ["id", "name", "path", "type", "size", "primary_entity", "updated_at"]
                    }
                    if file["type"] == 1:
                        queue.append(file["path"])
                    if len(files) > 50000:
                        raise OSError("本地版本单次目录扫描上限为 50000 项")
                heartbeat(phase="扫描文件目录", discovered=len(files))
                page = result["pagination"]
                if page.get("next_token"):
                    pagination = {"next_page_token": page["next_token"]}
                elif not page.get("is_cursor") and (page.get("page", 0) + 1) * page.get(
                    "page_size", 0
                ) < page.get("total_items", 0):
                    pagination = {"page": page["page"] + 1}
                else:
                    break
        return files

    @contextmanager
    def materialize(self, file, expected_sha=None, expected_size=None, heartbeat=lambda **_: None):
        if (
            expected_size is not None
            and shutil.disk_usage(tempfile.gettempdir()).free
            < expected_size + self.settings.disk_reserve_bytes
        ):
            raise OSError("解析临时磁盘空间不足")
        before = self.call("GET", "file/info", params={"uri": file["path"]})
        if before["id"] != file["id"] or before.get("primary_entity") != file.get("primary_entity"):
            raise OSError("文件版本已变化，等待下一轮同步")
        result = self.call(
            "POST",
            "file/url",
            json={"uris": [file["path"]], "download": True, "redirect": False, "no_cache": True},
        )
        url = result["urls"][0]["url"]
        allowed = {
            urlsplit(self.settings.native_cloudreve_url).netloc,
            urlsplit(self.settings.native_origin).netloc,
        }
        parsed_url = urlsplit(url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or parsed_url.netloc not in allowed
            or parsed_url.username
        ):
            raise OSError("当前版本只允许下载已配置文件服务上的原件")
        with tempfile.TemporaryDirectory(prefix="akb-native-") as directory:
            path = Path(directory) / "original"
            hasher, size = hashlib.sha256(), 0
            with self.client.stream("GET", url, follow_redirects=False) as response:
                response.raise_for_status()
                with path.open("wb") as stream:
                    for chunk in response.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > self.settings.upload_limit or (
                            expected_size is not None and size > expected_size
                        ):
                            raise OSError("文件大小超限或已变化")
                        stream.write(chunk)
                        hasher.update(chunk)
                        heartbeat(phase="核验文件内容", file=file["name"], bytes_read=size)
            sha = hasher.hexdigest()
            if (expected_size is not None and size != expected_size) or (
                expected_sha and sha != expected_sha
            ):
                raise OSError("文件内容与索引版本不一致，等待重新同步")
            after = self.call("GET", "file/info", params={"uri": file["path"]})
            if any(
                before.get(k) != after.get(k) for k in ["id", "primary_entity", "updated_at", "size", "path"]
            ):
                raise OSError("读取期间文件发生变化，未发布本次结果")
            heartbeat(phase="文件内容核验完成", file=file["name"], bytes_read=size)
            yield path, sha, size


def enqueue(session, tenant_id, actor="external"):
    services.lock_tenant(session, tenant_id)
    pending = session.scalar(
        select(Job)
        .where(Job.kind == "native_sync", Job.status.in_(["queued", "running"]))
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    if pending:
        return pending
    job = Job(
        tenant_id=tenant_id, source_id=None, kind="native_sync", result={"actor": actor, "phase": "等待扫描"}
    )
    session.add(job)
    session.flush()
    return job


def classify(before, after):
    if not before:
        return "create"
    if before.get("deleted"):
        return "restore"
    if before.get("sha256") != after.get("sha256"):
        return "content_update"
    if before.get("name") != after.get("name"):
        return "rename"
    if before.get("path") != after.get("path"):
        return "move"
    return "metadata"


def apply_file(session, settings, tenant_id, connection, file, sha, size, actor):
    tenant = services.lock_tenant(session, tenant_id)
    item = session.scalar(select(NativeFile).where(NativeFile.provider_file_id == file["id"]))
    if item is None:
        item = NativeFile(
            id=uid(),
            tenant_id=tenant_id,
            connection_id=connection.id,
            provider_file_id=file["id"],
            snapshot={},
        )
        session.add(item)
    before = item.snapshot
    after = {**file, "sha256": sha, "deleted": False}
    item.missing_count = 0
    effective_keys = ["id", "name", "path", "sha256", "size", "deleted", "type"]
    changed = any(before.get(k) != after.get(k) for k in effective_keys)
    source = session.get(Source, item.source_id) if item.source_id else None
    old_generation = source.generation_id if source else None
    if file["type"] == 0 and (changed or not source):
        delta = size - (source.size if source else 0)
        if tenant.used_bytes + tenant.reserved_bytes + tenant.memory_bytes + delta > tenant.quota_bytes:
            raise OSError("知识库配额不足；原文件保留，等待管理员调整配额后重试")
        if not source:
            source = Source(
                id=uid(),
                tenant_id=tenant_id,
                filename=file["name"],
                title=file["name"],
                media_type=mimetypes.guess_type(file["name"])[0] or "application/octet-stream",
                sha256=sha,
                size=size,
                object_key=f"native:{tenant_id}/{item.id}",
                origin_path=file["path"],
                subject_scope="reference",
                status="queued",
                indexed_hash="",
            )
            session.add(source)
            session.flush()
            item.source_id = source.id
        content_changed = not before or before.get("sha256") != sha
        source.filename = source.title = file["name"]
        source.origin_path, source.size, source.sha256 = file["path"], size, sha
        source.revoked_at = source.trashed_at = None
        source.revision += 1
        if content_changed or source.status in {"queued", "running", "revoked"}:
            source.generation_id = None
            source.status, source.error = "queued", None
            # Previous source-supported facts cannot survive a content replacement.
            for claim in session.scalars(select(Claim).where(Claim.source_id == source.id)):
                claim.status = "withdrawn"
            session.add(Job(tenant_id=tenant_id, source_id=source.id, source_revision=source.revision))
        tenant.used_bytes += delta
        tenant.revision += 1
        organization.enqueue(session, tenant_id)
    if changed:
        changes.record(
            session,
            tenant_id,
            file["id"],
            classify(before, after),
            actor,
            {**before, "generation_id": old_generation} if before else {},
            {**after, "generation_id": source.generation_id if source else None},
            item.source_id,
        )
    item.snapshot = after
    return changed


def sync(worker, ticket):
    tenant_id = ticket["tenant_id"]
    with worker.db.session(tenant_id) as session:
        connection = session.scalar(select(NativeConnection).where(NativeConnection.enabled.is_(True)))
        if not connection:
            raise OSError("文件空间尚未连接")
        connection_id = connection.id
        old = {f.provider_file_id: f.snapshot for f in session.scalars(select(NativeFile))}
        actor = session.get(Job, ticket["job_id"]).result.get("actor", "external")
    last_beat = 0

    def heartbeat(**progress):
        nonlocal last_beat
        if time.monotonic() - last_beat < 2:
            return
        with worker.db.session(tenant_id) as session:
            services.lock_tenant(session, tenant_id)
            job = worker._current_job(session, ticket)
            if not job:
                raise OSError("同步任务已过期或取消")
            job.lease_until = now() + timedelta(seconds=worker.settings.lease_seconds)
            job.result = {**job.result, **progress}
        last_beat = time.monotonic()

    provider = NativeClient(worker.settings, connection_id, tenant_id)
    try:
        inventory = provider.inventory(heartbeat)
        count, changed = 0, 0
        for file_id, file in inventory.items():
            before = old.get(file_id, {})
            sha, size = before.get("sha256", ""), file["size"]
            if file["type"] == 0 and (
                not sha or any(before.get(k) != file.get(k) for k in ["size", "primary_entity", "updated_at"])
            ):
                with provider.materialize(file, expected_size=size, heartbeat=heartbeat) as (_, sha, size):
                    pass
            with worker.db.session(tenant_id) as session:
                services.lock_tenant(session, tenant_id)
                if not worker._current_job(session, ticket):
                    return
                connection = session.get(NativeConnection, connection_id)
                changed += int(
                    apply_file(session, worker.settings, tenant_id, connection, file, sha, size, actor)
                )
            count += 1
            heartbeat(phase="更新资料关联", completed=count, total=len(inventory), file=file["name"])
        # Only a fully successful inventory may infer a deletion. Two scans absorb moves during listing.
        with worker.db.session(tenant_id) as session:
            tenant = services.lock_tenant(session, tenant_id)
            job = worker._current_job(session, ticket)
            if not job:
                return
            removed = False
            for item in session.scalars(select(NativeFile)):
                if item.provider_file_id in inventory or item.snapshot.get("deleted"):
                    continue
                item.missing_count += 1
                if item.missing_count < 2:
                    continue
                before = item.snapshot
                removed = True
                changed += 1
                item.snapshot = {**before, "deleted": True}
                if item.source_id:
                    source = session.get(Source, item.source_id)
                    source.revoked_at = source.trashed_at = now()
                    source.revision += 1
                    tenant.revision += 1
                changes.record(
                    session,
                    tenant_id,
                    item.provider_file_id,
                    "delete",
                    actor,
                    before,
                    item.snapshot,
                    item.source_id,
                )
            if removed:
                organization.enqueue(session, tenant_id)
            connection = session.get(NativeConnection, connection_id)
            connection.last_scan_at, connection.error = now(), None
            job.result = {
                **job.result,
                "phase": "扫描完成",
                "completed": count,
                "total": len(inventory),
                "changed": changed,
            }
            job.status, job.lease_until = "completed", None
    finally:
        provider.close()


@contextmanager
def local_original(settings, key, expected_sha, expected_size, heartbeat=lambda **_: None):
    tenant_id, file_id = key.removeprefix("native:").split("/")
    UUID(tenant_id), UUID(file_id)
    db = Database(settings.database_url)
    try:
        with db.session(tenant_id) as session:
            item = session.get(NativeFile, file_id)
            if not item or item.snapshot.get("deleted"):
                raise OSError("原文件已移入回收站或不存在")
            file, connection_id = item.snapshot, item.connection_id
        provider = NativeClient(settings, connection_id, tenant_id)
        try:
            with provider.materialize(file, expected_sha, expected_size, heartbeat=heartbeat) as (path, _, _):
                yield path
        finally:
            provider.close()
    finally:
        db.close()
