"""One authenticated library for the browser and WorkBuddy."""
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Query

from akb import local_catalog, native, native_start, services
from akb import memory_filling as filling
from akb import memory_library as memory
from akb.memory_evidence import EvidenceAnchor, resolve
from akb.memory_reparse import ReparseSource
from akb.memory_reparse import enqueue as enqueue_reparse
from akb.models import Job
from akb.schemas import Search
from akb.security import Principal, authenticate, require


def routes(app, db, settings):
    def scoped(p, write=False):
        require(p, "knowledge:read")
        require(p, "profile:read")
        with db.session(p.tenant_id) as session:
            memory.checked_binding(session, settings.knowledge_engine)
        if write:
            require(p, "knowledge:write")

    def drive_principal(x_cloudreve_token: str = Header(min_length=20, max_length=10000),
                        x_akb_file_user: str = Header(min_length=1, max_length=200)):
        identity = native.verify_identity(settings, x_cloudreve_token)
        if identity["id"] != x_akb_file_user:
            raise HTTPException(409, "文件账号已变化，请刷新")
        return native_start.principal(db, identity)

    def listing(p, offset=0, limit=50):
        scoped(p)
        with db.session(p.tenant_id) as session:
            return memory.library(session, p.tenant_id, offset, limit, engine=settings.knowledge_engine)

    @app.get("/v2/library")
    @app.get("/v1/catalog")
    @app.get("/v1/knowledge")
    def read_library(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                     p: Principal = Depends(authenticate)):
        return listing(p, offset, limit)

    @app.get("/v1/native/library")
    def native_library(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                       p: Principal = Depends(drive_principal)):
        return listing(p, offset, limit)

    @app.get("/v2/library/objects/{object_id}")
    def read(object_id: UUID, p: Principal = Depends(authenticate)):
        scoped(p)
        with db.session(p.tenant_id) as session:
            return memory.read_object(session, settings, p.tenant_id, str(object_id))

    @app.get("/v1/native/library/objects/{object_id}")
    def native_read(object_id: UUID, p: Principal = Depends(drive_principal)):
        return read(object_id, p)

    @app.post("/v2/library/sources/{source_id}/reparse", status_code=202)
    def reparse_source(source_id: UUID, body: ReparseSource, p: Principal = Depends(authenticate)):
        scoped(p, True)
        with db.session(p.tenant_id, actor=p.kind) as session:
            return enqueue_reparse(session, p, str(source_id), body.expected_revision)

    @app.post("/v2/library/objects")
    def publish(body: memory.MemoryWrite, p: Principal = Depends(authenticate)):
        scoped(p, True)
        with db.session(p.tenant_id, actor=p.kind) as session:
            result = memory.write(session, settings, p, body)
        return result

    @app.post("/v1/native/library/objects")
    def native_publish(body: memory.MemoryWrite, p: Principal = Depends(drive_principal)):
        return publish(body, p)

    @app.post("/v2/library/objects/{object_id}/forget")
    def forget(object_id: UUID, body: memory.MemoryDelete, p: Principal = Depends(authenticate)):
        scoped(p, True)
        with db.session(p.tenant_id, actor=p.kind) as session:
            result = memory.forget(session, p.tenant_id, str(object_id), body.expected_revision)
        # Tombstone is committed first; a network failure must never make forgotten content visible again.
        try:
            with db.session(p.tenant_id) as session:
                services.lock_tenant(session, p.tenant_id)
                memory.collect_garbage(session, settings, p.tenant_id)
                from sqlalchemy import select

                from akb.models import MemoryGarbage

                result["purge_pending"] = session.scalar(select(MemoryGarbage.id).limit(1)) is not None
        except HTTPException:
            pass
        return result

    @app.post("/v1/native/library/objects/{object_id}/forget")
    def native_forget(object_id: UUID, body: memory.MemoryDelete, p: Principal = Depends(drive_principal)):
        return forget(object_id, body, p)

    @app.post("/v2/library/search")
    @app.post("/v1/search")
    def search(body: Search, p: Principal = Depends(authenticate)):
        scoped(p)
        with db.session(p.tenant_id) as session:
            return memory.search(session, settings, p.tenant_id, body.query, body.limit)

    @app.post("/v2/library/evidence/resolve")
    def resolve_evidence(body: EvidenceAnchor, p: Principal = Depends(authenticate)):
        scoped(p)
        with db.session(p.tenant_id) as session:
            return resolve(session, body)

    @app.post("/v2/filling/prepare")
    def prepare_filling(body: filling.FillingPlan, p: Principal = Depends(authenticate)):
        scoped(p, True)
        with db.session(p.tenant_id, actor=p.kind) as session:
            return filling.prepare(session, p, body)

    @app.post("/v2/filling/{receipt_id}/validate")
    def validate_filling(receipt_id: UUID, body: filling.FillingCheck, p: Principal = Depends(authenticate)):
        scoped(p)
        with db.session(p.tenant_id) as session:
            return filling.validate(session, str(receipt_id), body)

    @app.get("/v2/library/health")
    def library_health(p: Principal = Depends(authenticate)):
        scoped(p)
        from akb.memory_runtime import health

        with db.session(p.tenant_id) as session:
            return health(session, settings, p.tenant_id)

    @app.post("/v2/library/sync")
    @app.post("/v1/catalog/organize")
    def sync(p: Principal = Depends(authenticate)):
        scoped(p, True)
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            # Reuse only the durable queue, not the old organizer.
            from akb.organization import enqueue

            job: Job = enqueue(session, p.tenant_id, force=True)
            return {"job_id": job.id, "model_executor": "workbuddy", "semantic_state": "awaiting_agent"}

    @app.post("/v2/local/catalog")
    def publish_local_catalog(body: local_catalog.CatalogSnapshot, p: Principal = Depends(authenticate)):
        scoped(p, True)
        with db.session(p.tenant_id, actor=p.kind) as session:
            return local_catalog.publish(session, p.tenant_id, body)

    @app.get("/v2/local/catalog")
    @app.get("/v2/local/catalog/{device_id}")
    def local_directory(device_id: UUID | None = None, offset: int = Query(0, ge=0),
                        limit: int = Query(50, ge=1, le=100), p: Principal = Depends(authenticate)):
        scoped(p)
        with db.session(p.tenant_id) as session:
            return local_catalog.listing(session, device_id, offset, limit)

    @app.get("/v1/native/local-catalog")
    def native_local_directory(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                               p: Principal = Depends(drive_principal)):
        return local_directory(None, offset, limit, p)

    @app.post("/v2/local/observe")
    def observe_local_sources(body: local_catalog.Observations, p: Principal = Depends(authenticate)):
        scoped(p, True)
        with db.session(p.tenant_id, actor=p.kind) as session:
            return local_catalog.observe(session, p.tenant_id, body)

    @app.post("/v2/local/match")
    def match_local_source(body: local_catalog.MatchSource, p: Principal = Depends(authenticate)):
        from sqlalchemy import select

        from akb.models import Source

        scoped(p)
        with db.session(p.tenant_id) as session:
            src = session.scalar(select(Source).where(Source.sha256 == body.sha256,
                Source.subject_scope == body.subject_scope, Source.revoked_at.is_(None), Source.trashed_at.is_(None),
                Source.status.in_(["queued", "processing", "ready", "partial"]), local_catalog.usable_sources()).limit(1))
            return {"source": services.source_dict(src) if src else None}
