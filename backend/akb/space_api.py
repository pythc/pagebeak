from fastapi import Depends, Header, HTTPException, Query, Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from akb import services, space
from akb.models import Catalog, CleanupPlan, StorageArchive, User
from akb.schemas import CleanupApproval, CleanupInput, QuotaInput, UploadInput
from akb.security import Principal, authenticate, browser_only, require


def routes(app, db, settings):
    @app.get("/v1/storage")
    def read_space(p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            return space.usage(session, settings, p.tenant_id)

    @app.get("/v1/storage/candidates")
    def cleanup_candidates(p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            return {"items": space.candidates(session), "automatic_deletion": False}

    @app.post("/v1/uploads", status_code=201)
    def start(body: UploadInput, p: Principal = Depends(authenticate),
              idempotency_key: str | None = Header(default=None, min_length=1, max_length=120)):
        require(p, "knowledge:write")
        with db.session(p.tenant_id, actor=p.kind) as session:
            return space.upload_dict(space.begin_upload(session, settings, p.tenant_id, body, idempotency_key))

    @app.get("/v1/uploads/{upload_id}")
    def upload_state(upload_id: str, p: Principal = Depends(authenticate)):
        require(p, "knowledge:write")
        with db.session(p.tenant_id, actor=p.kind) as session:
            return space.upload_dict(space.get_upload(session, upload_id))

    @app.put("/v1/uploads/{upload_id}/content")
    async def chunk(
        upload_id: str, request: Request, offset: int = Query(ge=0), p: Principal = Depends(authenticate)
    ):
        require(p, "knowledge:write")
        data = await request.body()

        def persist():
            with db.session(p.tenant_id, actor=p.kind) as session:
                return space.append_chunk(session, settings, p.tenant_id, upload_id, offset, data)

        return await run_in_threadpool(persist)

    @app.post("/v1/uploads/{upload_id}/complete", status_code=202)
    def complete(upload_id: str, p: Principal = Depends(authenticate)):
        require(p, "knowledge:write")
        with db.session(p.tenant_id, actor=p.kind) as session:
            result = space.finish_upload(session, settings, p.tenant_id, upload_id)
            path = space.staging_path(settings, space.get_upload(session, upload_id))
        path.unlink(missing_ok=True)
        return result

    @app.delete("/v1/uploads/{upload_id}")
    def cancel(upload_id: str, p: Principal = Depends(authenticate)):
        require(p, "knowledge:write")
        with db.session(p.tenant_id, actor=p.kind) as session:
            return space.cancel_upload(session, settings, p.tenant_id, upload_id)

    @app.get("/v1/storage/plans")
    def plans(p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            return {
                "items": [
                    space.plan_dict(plan)
                    for plan in session.scalars(
                        select(CleanupPlan).order_by(CleanupPlan.created_at.desc()).limit(50)
                    )
                ]
            }

    @app.post("/v1/storage/plans", status_code=201)
    def propose(body: CleanupInput, p: Principal = Depends(authenticate)):
        require(p, "knowledge:write")
        with db.session(p.tenant_id, actor=p.kind) as session:
            return space.plan_dict(space.create_plan(session, p.tenant_id, body))

    @app.get("/v1/storage/plans/{plan_id}")
    def read_plan(plan_id: str, p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            return space.plan_dict(space.get_plan(session, plan_id))

    @app.post("/v1/storage/plans/{plan_id}/approve")
    def approve(plan_id: str, body: CleanupApproval, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session(p.tenant_id, actor=p.kind) as session:
            return space.plan_dict(
                space.approve_plan(session, p.tenant_id, plan_id, p.user_id, body.acknowledge_loss)
            )

    @app.post("/v1/storage/plans/{plan_id}/execute")
    def execute(plan_id: str, p: Principal = Depends(authenticate)):
        require(p, "knowledge:write")
        with db.session(p.tenant_id, actor=p.kind) as session:
            return space.plan_dict(space.execute_plan(session, p.tenant_id, plan_id))

    @app.post("/v1/storage/plans/{plan_id}/cancel")
    def cancel_plan(plan_id: str, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            plan = space.get_plan(session, plan_id)
            if plan.status == "completed":
                raise HTTPException(409, "方案已经执行")
            plan.status = "cancelled"
            services.audit(session, p.tenant_id, "storage.plan.cancel", plan.id)
            return space.plan_dict(plan)

    @app.get("/v1/storage/archives")
    def archives(
        offset: int = Query(0, ge=0),
        limit: int = Query(10, ge=1, le=20),
        query: str = Query("", max_length=200),
        p: Principal = Depends(authenticate),
    ):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            rows = list(
                session.scalars(
                    select(StorageArchive)
                    .where(StorageArchive.content.contains(query, autoescape=True))
                    .order_by(StorageArchive.created_at.desc(), StorageArchive.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return {
                "items": [
                    {
                        "id": a.id,
                        "title": a.title,
                        "content": a.content,
                        "manifest": a.manifest,
                        "kind": "lossy_archive",
                        "created_at": a.created_at,
                    }
                    for a in rows[:limit]
                ],
                "offset": offset,
                "limit": limit,
                "has_more": len(rows) > limit,
            }

    @app.delete("/v1/storage/archives/{archive_id}", status_code=204)
    def delete_archive(archive_id: str, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            archive = session.get(StorageArchive, archive_id)
            if not archive:
                raise HTTPException(404, "摘要归档不存在")
            # Remove the preview snapshot as well, so privacy deletion actually removes retained text.
            plan = session.get(CleanupPlan, archive.plan_id)
            plan.summary, plan.manifest = "", []
            session.delete(archive)
            session.scalar(select(Catalog)).revision += 1
            services.audit(session, p.tenant_id, "storage.archive.delete", archive_id)

    def administrator(p):
        browser_only(p)
        if not p.is_admin:
            raise HTTPException(403, "仅管理员可以分配用户空间")

    @app.get("/v1/admin/storage/users")
    def list_users(offset: int = Query(0, ge=0), p: Principal = Depends(authenticate)):
        administrator(p)
        with db.session() as session:
            users = list(session.scalars(select(User).order_by(User.id).offset(offset).limit(100)))
        result = []
        for user in users:
            with db.session(user.tenant_id) as session:
                result.append(
                    {
                        "id": user.id,
                        "email": user.email,
                        "display_name": user.display_name,
                        **space.usage(session, settings, user.tenant_id),
                    }
                )
        return {"items": result, "offset": offset, "has_more": len(result) == 100}

    @app.patch("/v1/admin/storage/users/{user_id}")
    def allocate(user_id: str, body: QuotaInput, p: Principal = Depends(authenticate)):
        administrator(p)
        with db.session() as session:
            user = session.get(User, user_id)
            if not user:
                raise HTTPException(404, "用户不存在")
        with db.session(user.tenant_id) as session:
            tenant = services.lock_tenant(session, user.tenant_id)
            space.expire_uploads(session, settings, tenant)
            if tenant.quota_bytes != body.expected_quota_bytes:
                raise HTTPException(409, "配额已由其他管理员修改，请刷新")
            if body.quota_bytes < tenant.used_bytes + tenant.reserved_bytes + tenant.memory_bytes:
                raise HTTPException(409, "新配额不能小于已用与预留空间之和")
            tenant.quota_bytes = body.quota_bytes
            services.audit(session, user.tenant_id, "storage.quota.allocate", p.user_id)
            return space.usage(session, settings, user.tenant_id)
