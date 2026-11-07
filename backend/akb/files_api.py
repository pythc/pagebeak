"""Small, Chinese file workspace over tenant-scoped identities, never a generic provider proxy."""

from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from akb import changes, organization, services, space
from akb.models import CatalogEntry, CleanupPlan, FileFolder, Job, Source, Upload, now
from akb.security import Principal, authenticate, browser_only, require


class FolderInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_id: str | None = None


class EditFile(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    folder_id: str | None = None


class FileAction(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    action: Literal["move", "trash", "restore", "purge"]
    folder_id: str | None = None


def valid_name(name):
    name = name.strip()
    if (
        not name
        or name in {".", ".."}
        or any(c in name for c in "/\\\x00\r\n")
        or any(ord(c) < 32 for c in name)
    ):
        raise HTTPException(422, "名称不能为空，也不能含路径分隔符或控制字符")
    return name


def folder(session, folder_id):
    if folder_id is None:
        return None
    item = session.get(FileFolder, folder_id)
    if not item:
        raise HTTPException(404, "文件夹不存在")
    return item


def routes(app, db, settings):
    @app.get("/v1/files")
    def listing(
        folder_id: str | None = None,
        trash: bool = False,
        query: str = Query("", max_length=200),
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
        p: Principal = Depends(authenticate),
    ):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            parent = folder(session, folder_id)
            breadcrumbs = []
            while parent:
                if len(breadcrumbs) >= 100:
                    raise HTTPException(409, "文件夹层级异常，请联系管理员")
                breadcrumbs.insert(0, {"id": parent.id, "name": parent.name})
                parent = folder(session, parent.parent_id)
            q = select(Source).where(Source.trashed_at.is_not(None) if trash else Source.revoked_at.is_(None))
            if query.strip():
                q = q.where(Source.filename.contains(query.strip(), autoescape=True))
            elif not trash:
                q = q.where(Source.folder_id == folder_id)
            total = session.scalar(select(func.count()).select_from(q.subquery()))
            sources = list(
                session.scalars(q.order_by(Source.created_at.desc(), Source.id).offset(offset).limit(limit))
            )
            entries = {
                e.source_id: e
                for e in session.scalars(
                    select(CatalogEntry).where(CatalogEntry.source_id.in_([s.id for s in sources]))
                )
            }
            folders = (
                []
                if trash or query.strip()
                else list(
                    session.scalars(
                        select(FileFolder).where(FileFolder.parent_id == folder_id).order_by(FileFolder.name)
                    )
                )
            )
            return {
                "items": [
                    {
                        **services.source_dict(s),
                        "summary": organization.entry_dict(s, entries.get(s.id))["summary"],
                    }
                    for s in sources
                ],
                "folders": [{"id": f.id, "name": f.name, "parent_id": f.parent_id} for f in folders],
                "breadcrumbs": breadcrumbs,
                "total": total,
                "offset": offset,
                "limit": limit,
                "storage": space.usage(session, settings, p.tenant_id),
                "provider": "cloudreve" if settings.cloudreve_credentials else "local",
            }

    @app.get("/v1/files/folders")
    def folders(p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            return {
                "items": [
                    {"id": f.id, "name": f.name, "parent_id": f.parent_id}
                    for f in session.scalars(select(FileFolder).order_by(FileFolder.name))
                ]
            }

    @app.post("/v1/files/folders", status_code=201)
    def create_folder(body: FolderInput, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            parent = folder(session, body.parent_id)
            depth = 0
            while parent:
                depth += 1
                if depth >= 100:
                    raise HTTPException(422, "文件夹层级最多 100 层")
                parent = folder(session, parent.parent_id)
            name = valid_name(body.name)
            if session.scalar(
                select(FileFolder.id).where(
                    FileFolder.parent_id == body.parent_id, func.lower(FileFolder.name) == name.lower()
                )
            ):
                raise HTTPException(409, "同名文件夹已存在")
            item = FileFolder(tenant_id=p.tenant_id, name=name, parent_id=body.parent_id)
            session.add(item)
            session.flush()
            services.audit(session, p.tenant_id, "folder.create", item.id)
            return {"id": item.id, "name": item.name}

    @app.patch("/v1/files/folders/{folder_id}")
    def edit_folder(folder_id: str, body: FolderInput, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            item = folder(session, folder_id)
            parent = folder(session, body.parent_id)
            depth = 0
            while parent:
                depth += 1
                if parent.id == item.id or depth > 100:
                    raise HTTPException(409, "不能把文件夹移动到自己或子文件夹中")
                parent = folder(session, parent.parent_id)
            descendants, level = [item.id], 1
            all_folders = list(session.scalars(select(FileFolder)))
            while descendants:
                if depth + level > 100:
                    raise HTTPException(422, "移动后文件夹层级将超过 100 层")
                descendants = [f.id for f in all_folders if f.parent_id in descendants]
                level += 1
            name = valid_name(body.name)
            if session.scalar(
                select(FileFolder.id).where(
                    FileFolder.id != item.id,
                    FileFolder.parent_id == body.parent_id,
                    func.lower(FileFolder.name) == name.lower(),
                )
            ):
                raise HTTPException(409, "同名文件夹已存在")
            item.name, item.parent_id = name, body.parent_id
            services.audit(session, p.tenant_id, "folder.edit", item.id)
            return {"id": item.id, "name": item.name}

    @app.delete("/v1/files/folders/{folder_id}", status_code=204)
    def remove_folder(folder_id: str, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            item = folder(session, folder_id)
            if (
                session.scalar(
                    select(Source.id).where(Source.folder_id == folder_id, Source.object_key != "")
                )
                or session.scalar(select(FileFolder.id).where(FileFolder.parent_id == folder_id))
                or session.scalar(
                    select(Upload.id).where(Upload.folder_id == folder_id, Upload.status == "staging")
                )
            ):
                raise HTTPException(409, "文件夹内仍有文件或子文件夹，请先移出（含回收站中的文件）")
            for source in session.scalars(select(Source).where(Source.folder_id == folder_id)):
                source.folder_id = None
            session.delete(item)
            services.audit(session, p.tenant_id, "folder.delete", folder_id)

    @app.patch("/v1/files/{source_id}")
    def edit_file(source_id: str, body: EditFile, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session(p.tenant_id, actor=p.kind) as session:
            tenant = services.lock_tenant(session, p.tenant_id)
            source = services.get_source(session, source_id)
            if source.object_key.startswith("native:"):
                raise HTTPException(409, "请在原生文件管理页面修改，知识库会自动同步")
            before = changes.snapshot(source)
            if "folder_id" in body.model_fields_set:
                folder(session, body.folder_id)
                source.folder_id = body.folder_id
            if body.name is not None:
                name = valid_name(body.name)
                old_suffix = source.filename.rpartition(".")[2] if "." in source.filename else ""
                if old_suffix and not name.lower().endswith("." + old_suffix.lower()):
                    raise HTTPException(422, "请保留原文件扩展名")
                source.filename, source.title = name, name
                source.revision += 1
                # Re-fence ingestion after a metadata edit; completed evidence remains valid.
                if source.status in {"queued", "running"}:
                    session.add(
                        Job(tenant_id=p.tenant_id, source_id=source.id, source_revision=source.revision)
                    )
                tenant.revision += 1
                organization.enqueue(session, p.tenant_id)
            services.audit(session, p.tenant_id, "file.edit", source.id)
            if before != changes.snapshot(source):
                changes.record(
                    session,
                    p.tenant_id,
                    source.id,
                    "rename" if body.name is not None else "move",
                    p.kind,
                    before,
                    changes.snapshot(source),
                    source.id,
                )
                organization.enqueue(session, p.tenant_id)
            return services.source_dict(source)

    @app.post("/v1/files/actions")
    def actions(body: FileAction, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session(p.tenant_id, actor=p.kind) as session:
            tenant = services.lock_tenant(session, p.tenant_id)
            if body.action == "move":
                folder(session, body.folder_id)
            for source_id in dict.fromkeys(body.ids):
                source = session.get(Source, source_id)
                if not source or (source.revoked_at and not source.trashed_at):
                    raise HTTPException(404, "文件不存在")
                if source.object_key.startswith("native:"):
                    raise HTTPException(409, "请在原生文件管理页面操作，知识库会自动同步")
                before = changes.snapshot(source)
                if body.action == "move":
                    if source.trashed_at:
                        raise HTTPException(409, "请先恢复文件")
                    source.folder_id = body.folder_id
                elif body.action == "trash":
                    if source.trashed_at:
                        continue
                    source.trashed_at = source.revoked_at = now()
                    source.revision += 1
                    for plan in session.scalars(
                        select(CleanupPlan).where(CleanupPlan.status.in_(["pending", "authorized"]))
                    ):
                        if any(item["id"] == source.id for item in plan.manifest):
                            plan.status, plan.summary, plan.manifest = "cancelled", "", []
                    for job in session.scalars(
                        select(Job)
                        .where(Job.source_id == source.id, Job.status.in_(["queued", "running"]))
                        .with_for_update()
                    ):
                        job.status, job.fencing_token = "cancelled", job.fencing_token + 1
                elif body.action == "restore":
                    if not source.trashed_at:
                        raise HTTPException(409, "文件不在回收站")
                    source.trashed_at = source.revoked_at = None
                    source.revision += 1
                    if source.status in {"queued", "running"}:
                        session.add(
                            Job(tenant_id=p.tenant_id, source_id=source.id, source_revision=source.revision)
                        )
                else:
                    if not source.trashed_at:
                        raise HTTPException(409, "请先将文件移入回收站")
                    source.trashed_at = None
                    source.status = "revoked"
                    source.revision += 1
                    session.add(
                        Job(
                            tenant_id=p.tenant_id,
                            source_id=source.id,
                            source_revision=source.revision,
                            kind="cleanup",
                        )
                    )
                services.audit(session, p.tenant_id, "file." + body.action, source.id)
                changes.record(
                    session,
                    p.tenant_id,
                    source.id,
                    body.action,
                    p.kind,
                    before,
                    changes.snapshot(source),
                    source.id,
                )
                organization.enqueue(session, p.tenant_id)
            if body.action != "move":
                tenant.revision += 1
                organization.enqueue(session, p.tenant_id)
            return {"status": "completed", "count": len(set(body.ids))}
