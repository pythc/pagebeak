"""The browser and MCP use the same knowledge, task and decision services."""

from fastapi import Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy import select

from akb import knowledge, services
from akb.knowledge_schema import KnowledgeAcceptance, KnowledgeDecisionInput, KnowledgeSubmission
from akb.models import Catalog, FormDraft
from akb.schemas import FormOutput
from akb.security import Principal, authenticate, browser_only, require


def routes(app, db):
    @app.post("/v1/forms/drafts/{draft_id}/validate")
    def validate(draft_id: str, body: FormOutput, p: Principal = Depends(authenticate)):
        require(p, "forms:write")
        from akb.form_answers import validate_output

        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            draft = session.get(FormDraft, draft_id)
            if not draft:
                raise HTTPException(404, "草稿不存在")
            return validate_output(services.draft_dict(session, p.tenant_id, draft), body.values)

    @app.post("/v1/forms/drafts/{draft_id}/document")
    async def document(draft_id: str, file: UploadFile = File(), p: Principal = Depends(authenticate)):
        require(p, "forms:write")
        data = await file.read(10 * 1024**2 + 1)
        await file.close()
        if len(data) > 10 * 1024**2:
            raise HTTPException(413, "填写模板最高 10 MiB；资料入库仍支持 2 GiB")
        from akb.form_document import fill_template

        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            draft = session.get(FormDraft, draft_id)
            if not draft:
                raise HTTPException(404, "草稿不存在")
            current = services.draft_dict(session, p.tenant_id, draft)
            if current["stale"]:
                raise HTTPException(409, "资料已变化，请重新生成填写草稿")
            document_bytes = fill_template(data, current["answers"])
            return Response(
                document_bytes,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                headers={
                    "Content-Disposition": 'attachment; filename="filled-draft.docx"',
                    "X-Knowledge-Revision": str(current["snapshot_revision"]),
                },
            )

    @app.get("/v1/context")
    def context(p: Principal = Depends(authenticate)):
        with db.session(p.tenant_id) as session:
            catalog = session.scalar(select(Catalog))
            return {
                "knowledge_base_id": p.tenant_id,
                "user_id": p.user_id,
                "display_name": p.display_name,
                "catalog_id": catalog.id,
                "scopes": sorted(p.scopes),
                "contract": {"tencent": "tencent-workbuddy-v4", "weknora": "weknora-workbuddy-v1"}.get(app.state.settings.knowledge_engine, "evidence-knowledge-v1"),
                "knowledge_engine": app.state.settings.knowledge_engine,
            }

    @app.get("/v1/knowledge")
    def read_knowledge(
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
        p: Principal = Depends(authenticate),
    ):
        require(p, "knowledge:read")
        require(p, "profile:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return knowledge.projection(session, p.tenant_id, offset, limit)

    @app.get("/v1/knowledge/tasks")
    def read_tasks(
        offset: int = Query(0, ge=0),
        limit: int = Query(30, ge=1, le=100),
        p: Principal = Depends(authenticate),
    ):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return knowledge.tasks(session, offset, limit)

    @app.post("/v1/knowledge/tasks/{task_id}/claim")
    def claim(task_id: str, p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        require(p, "knowledge:write")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return knowledge.claim_task(session, p.tenant_id, task_id, p.credential_id)

    @app.post("/v1/knowledge/tasks/{task_id}/submit")
    def submit(task_id: str, body: KnowledgeSubmission, p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        require(p, "knowledge:write")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return knowledge.submit_task(session, p.tenant_id, task_id, p.credential_id, body)

    @app.post("/v1/knowledge/facts/{fact_id}/decisions")
    def decide(fact_id: str, body: KnowledgeDecisionInput, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return knowledge.decide(session, p.tenant_id, fact_id, body)

    @app.post("/v1/knowledge/accept")
    def agent_accept(body: KnowledgeAcceptance, p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        require(p, "knowledge:write")
        require(p, "profile:read")
        if p.kind != "api":
            raise HTTPException(403, "Agent 采纳使用知识库连接；网页仍使用人工决定入口")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return knowledge.accept_by_agent(session, p.tenant_id, p.credential_id, body)
