import re
import secrets
import sys
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from starlette.background import BackgroundTask
from starlette.middleware.cors import CORSMiddleware

from akb import organization, services
from akb.config import ROOT, Settings
from akb.db import Database
from akb.middleware import BodyLimitMiddleware
from akb.models import Catalog, Credential, FormDraft, Job, Onboarding, Source, Tenant, User, uid
from akb.schemas import (
    CatalogEdit,
    CatalogEntryEdit,
    DecisionInput,
    FormInput,
    Login,
    PageInput,
    Proposals,
    Register,
    Search,
    TextImport,
    TokenRequest,
)
from akb.security import (
    Principal,
    authenticate,
    browser_only,
    issue_credential,
    password_hash,
    password_matches,
    require,
)


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    if settings.identity_required and not (settings.passwordless_enabled and settings.assistant_public_origin):
        raise ValueError('外部账号身份需要启用个人空间入口并配置公开 MCP 地址')
    db = Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            if getattr(app.state, "assistant_mcp", None):
                await stack.enter_async_context(app.state.assistant_mcp.session_manager.run())
            try:
                yield
            finally:
                db.close()

    app = FastAPI(title="Agent 个人知识库", version="0.1.0", lifespan=lifespan)
    app.state.db, app.state.settings = db, settings
    auth_attempts = defaultdict(deque)

    @app.middleware("http")
    async def request_guards(request: Request, call_next):
        request_id = str(uuid4())
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > settings.upload_limit + 100000):
            return JSONResponse({"detail": "请求内容超过本地版本大小限制", "request_id": request_id}, 413)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            allowed = {settings.web_origin, str(request.base_url).rstrip("/")}
            if request.url.path.startswith(("/v1/native/library", "/v1/native/assistant/")):
                allowed.add(settings.native_origin)
            if request.url.path in {"/v1/native/connect", "/v1/native/wake", "/v1/native/notice",
                                    "/v1/native/start/setup", "/v1/native/start/package", "/v1/native/start/launch", "/v1/native/preview"}:
                allowed.add(settings.native_origin)
            if origin and origin not in allowed:
                return JSONResponse({"detail": "来源未获允许", "request_id": request_id}, 403)
        if (request.url.path.startswith('/v1/welcome') or request.url.path in {"/v1/auth/login", "/v1/auth/register", "/v1/onboarding/redeem", "/v1/onboarding/connection",
                                "/v1/onboarding/package", "/v1/onboarding/package/launch", "/v1/native/start/setup",
                                "/v1/native/start/package", "/v1/native/start/launch", "/register", "/authorize", "/token", "/revoke"}) and (request.method == "POST" or request.url.path in {'/v1/welcome', '/v1/welcome/identity/callback'}):
            host = request.client.host if request.client else "unknown"
            limit = 300 if settings.passwordless_enabled else 30
            if request.url.path.startswith('/v1/welcome') and request.cookies.get('zhixu_device'):
                from akb.security import digest_token
                host = 'device:' + digest_token(request.cookies['zhixu_device'])
                limit = 30
            attempts = auth_attempts[host]
            if len(auth_attempts) > 10000:
                auth_attempts.clear()
            cutoff = time.monotonic() - 60
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if len(attempts) >= limit:
                return JSONResponse({"detail": "连接请求过于频繁，请稍后重试"}, 429)
            attempts.append(time.monotonic())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(IntegrityError)
    async def integrity_error(_request, _exc):
        return JSONResponse({"detail": "数据发生冲突，请刷新后重试"}, 409)

    @app.get("/healthz")
    def health():
        with db.session() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/v1/capabilities")
    def capabilities(p: Principal = Depends(authenticate)):
        if settings.knowledge_engine in {"tencent", "weknora"}:
            return {"knowledge_engine": settings.knowledge_engine, "model_executor": "workbuddy",
                    "inference_mode": "agent_via_mcp", "background_model_api": False,
                    "upload_limit_bytes": settings.upload_limit, "parser": "local-office-ocr",
                    "storage_quotas": True, "legacy_knowledge_engine": False}
        return {
            "parser": "local-baseline",
            "retrieval": "lexical",
            "ocr": "rapidocr_onnxruntime_local_cpu",
            "llm": False,
            "ragflow": "not_connected",
            "workbuddy": "mcp_stdio",
            "network_enrichment": False,
            "catalog": "document-extracts-catalog-v2",
            "upload_limit_bytes": settings.upload_limit,
            "storage_quotas": True,
            "cleanup_approval": "exact_plan_in_browser",
            "automatic_incremental_organization": True,
            "semantic_organization": "leased_agent_tasks",
            "knowledge_facts": "evidence-knowledge-v1",
            "external_form_submission": False,
            "file_workspace": True,
            "original_storage": "cloudreve_gateway" if settings.cloudreve_credentials else "local",
        }

    def session_response(response, user, session):
        token, credential = issue_credential(session, user.id, "browser")
        response.set_cookie(
            "akb_session",
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=7 * 86400,
            path="/",
        )
        return {
            "id": user.id,
            "display_name": user.display_name,
            "csrf": credential.csrf,
            "is_admin": user.is_admin,
        }

    @app.post("/v1/auth/register", status_code=201)
    def register(body: Register, response: Response):
        if settings.identity_required:
            raise HTTPException(404, '请使用已有账号授权，无需另行注册')
        email = body.email.strip().casefold()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or not body.display_name.strip():
            raise HTTPException(422, "请输入有效邮箱和姓名")
        with db.session() as session:
            if session.scalar(select(User.id).where(User.email == email)):
                raise HTTPException(409, "该邮箱已注册")
            tenant_id = uid()
            user = User(
                email=email,
                display_name=body.display_name.strip(),
                password_hash=password_hash(body.password),
                tenant_id=tenant_id,
            )
            session.add(user)
            session.flush()
            session.execute(text("SELECT set_config('akb.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            session.add(Tenant(id=tenant_id, quota_bytes=settings.default_quota_bytes))
            session.flush()
            session.add(Catalog(tenant_id=tenant_id))
            session.add(Onboarding(tenant_id=tenant_id))
            session.flush()
            return session_response(response, user, session)

    dummy_hash = password_hash(secrets.token_urlsafe(20))

    @app.post("/v1/auth/login")
    def login(body: Login, response: Response):
        if settings.identity_required:
            raise HTTPException(404, '请使用已有账号授权')
        with db.session() as session:
            user = session.scalar(select(User).where(User.email == body.email.strip().casefold()))
            valid = password_matches(body.password, user.password_hash if user else dummy_hash)
            if not user or not valid:
                raise HTTPException(401, "邮箱或密码不正确")
            return session_response(response, user, session)

    @app.get("/v1/auth/me")
    def me(p: Principal = Depends(authenticate)):
        return {
            "id": p.user_id,
            "display_name": p.display_name,
            "csrf": p.csrf if p.kind == "browser" else None,
            "kind": p.kind,
            "scopes": sorted(p.scopes),
            "is_admin": p.is_admin,
        }

    @app.post("/v1/auth/logout", status_code=204)
    def logout(response: Response, p: Principal = Depends(authenticate)):
        with db.session() as session:
            session.get(Credential, p.credential_id).revoked = True
        response.delete_cookie("akb_session", path="/")

    @app.post("/v1/tokens", status_code=201)
    def create_token(body: TokenRequest, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session() as session:
            token, credential = issue_credential(session, p.user_id, "api", body.label, body.scopes)
            return {
                "id": credential.id,
                "token": token,
                "scopes": credential.scopes,
                "expires_at": credential.expires_at,
            }

    @app.get("/v1/tokens")
    def list_tokens(p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session() as session:
            return [
                {
                    "id": c.id,
                    "label": c.label,
                    "scopes": c.scopes,
                    "expires_at": c.expires_at,
                    "revoked": c.revoked,
                }
                for c in session.scalars(
                    select(Credential).where(Credential.user_id == p.user_id, Credential.kind == "api")
                )
            ]

    @app.get("/v1/integrations/workbuddy/config")
    def workbuddy_config(p: Principal = Depends(authenticate)):
        browser_only(p)
        if settings.assistant_public_origin:
            from akb.assistant_auth import public_origin
            from akb.assistant_package import NAME

            return {"mcpServers": {NAME: {"type": "streamableHttp",
                                          "url": public_origin(settings.assistant_public_origin) + "/mcp"}}}
        return {
            "mcpServers": {
                "personal-knowledge": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": ["-m", "akb.mcp_server"],
                    "cwd": str(ROOT),
                    "env": {
                        "AKB_API_URL": f"http://127.0.0.1:{settings.api_port}",
                        "AKB_TOKEN": "填写你在此页面生成的令牌",
                        "AKB_EXPECTED_KB_ID": p.tenant_id,
                    },
                }
            }
        }

    @app.delete("/v1/tokens/{token_id}", status_code=204)
    def revoke_token(token_id: str, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session() as session:
            credential = session.scalar(
                select(Credential).where(
                    Credential.id == token_id, Credential.user_id == p.user_id, Credential.kind == "api"
                )
            )
            if not credential:
                raise HTTPException(404, "令牌不存在")
            credential.revoked = True

    def ingest(data, filename, media_type, scope, key, p):
        require(p, "knowledge:write")
        if not data or len(data) > settings.upload_limit:
            raise HTTPException(413, "文件为空或超过 2 GiB")
        if key and (len(key) > 120 or not key.strip()):
            raise HTTPException(422, "幂等键长度不合法")
        with db.session(p.tenant_id, actor=p.kind) as session:
            source, job, replayed = services.register_source(
                session, settings, p.tenant_id, filename, media_type, data, scope, key
            )
            return {"source": services.source_dict(source), "job_id": job.id, "replayed": replayed}

    @app.post("/v1/sources/upload", status_code=202)
    async def upload(
        file: UploadFile = File(),
        subject_scope: str = Form("reference"),
        idempotency_key: str | None = Header(default=None),
        p: Principal = Depends(authenticate),
    ):
        if subject_scope not in {"self", "reference"}:
            raise HTTPException(422, "资料主体类型不合法")
        data = await file.read(10 * 1024**2 + 1)
        await file.close()
        if len(data) > 10 * 1024**2:
            raise HTTPException(413, "大文件请使用分块上传接口，单文件最高 2 GiB")
        return ingest(
            data,
            file.filename or "未命名文件",
            file.content_type or "application/octet-stream",
            subject_scope,
            idempotency_key,
            p,
        )

    @app.post("/v1/sources/text", status_code=202)
    def import_text(
        body: TextImport,
        idempotency_key: str | None = Header(default=None),
        p: Principal = Depends(authenticate),
    ):
        filename = Path(body.title).name
        if not filename.endswith((".txt", ".md")):
            filename += ".txt"
        return ingest(body.text.encode(), filename, "text/plain", body.subject_scope, idempotency_key, p)

    @app.get("/v1/sources")
    def sources(
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
        p: Principal = Depends(authenticate),
    ):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            rows = session.scalars(
                select(Source)
                .where(Source.revoked_at.is_(None))
                .order_by(Source.created_at.desc(), Source.id)
                .offset(offset)
                .limit(limit)
            )
            return {"items": [services.source_dict(s) for s in rows], "offset": offset, "limit": limit}

    @app.get("/v1/sources/{source_id}")
    def source(source_id: str, p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return services.source_dict(services.get_source(session, source_id))

    @app.get("/v1/sources/{source_id}/evidence")
    def source_evidence(
        source_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(10, ge=1, le=30),
        p: Principal = Depends(authenticate),
    ):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            services.get_source(session, source_id)
            rows = session.scalars(
                services.evidence_query()
                .where(services.Evidence.source_id == source_id)
                .order_by(services.Evidence.ordinal)
                .offset(offset)
                .limit(limit)
            )
            return {
                "items": [services.evidence_dict(session, e) for e in rows],
                "offset": offset,
                "limit": limit,
            }

    @app.get("/v1/sources/{source_id}/original")
    def download(source_id: str, request: Request, p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            source = services.get_source(session, source_id)
            filename = source.filename
            if source.object_key.startswith("native:"):
                from akb.object_store import local_original

                context = local_original(settings, source.object_key, source.sha256, source.size)
                try:
                    path = context.__enter__()
                except (OSError, httpx.HTTPError) as exc:
                    raise HTTPException(409, "原件已变化或暂不可用，请等待同步后重试") from exc
                return FileResponse(
                    path, filename=filename, background=BackgroundTask(context.__exit__, None, None, None)
                )
            if source.object_key.startswith("cloudreve:"):
                from akb.object_store import Cloudreve

                try:
                    provider = Cloudreve(settings)
                except OSError as exc:
                    raise HTTPException(503, "文件存储暂不可用，请稍后重试") from exc
                try:
                    req = provider.client.build_request(
                        "GET",
                        provider.path(source.object_key),
                        headers={
                            **({"Range": request.headers["range"]} if "range" in request.headers else {}),
                        },
                    )
                    # Explicit authorization; httpx build_request has no per-request auth argument.
                    auth = httpx.BasicAuth(provider.config["email"], provider.config["dav_password"])
                    upstream = provider.client.send(req, stream=True, auth=auth)
                    if upstream.status_code not in {200, 206, 416}:
                        upstream.close()
                        raise OSError("Provider unavailable")
                except (OSError, httpx.HTTPError) as exc:
                    provider.close()
                    raise HTTPException(503, "文件存储暂不可用，请稍后重试") from exc

                def close():
                    upstream.close()
                    provider.close()

                headers = {
                    k: v
                    for k, v in upstream.headers.items()
                    if k in {"content-length", "content-range", "accept-ranges"}
                }
                headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(filename, safe="")
                return StreamingResponse(
                    upstream.iter_bytes(),
                    status_code=upstream.status_code,
                    headers=headers,
                    media_type="application/octet-stream",
                    background=BackgroundTask(close),
                )
            try:
                path = services.object_path(settings, source.object_key)
                if not path.is_file():
                    raise OSError()
            except OSError as exc:
                raise HTTPException(404, "原件暂不可用") from exc
            return FileResponse(path, media_type="application/octet-stream", filename=filename)

    @app.delete("/v1/sources/{source_id}", status_code=202)
    def delete_source(source_id: str, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.revoke_source(session, p.tenant_id, source_id)
        return {"status": "revoked", "cleanup": "queued"}

    @app.get("/v1/evidence/{evidence_id}")
    def evidence(evidence_id: str, p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return services.evidence_dict(session, services.get_evidence(session, evidence_id))

    @app.get("/v1/jobs/{job_id}")
    def job_status(job_id: str, p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            job = session.get(Job, job_id)
            if not job:
                raise HTTPException(404, "任务不存在")
            return {
                "id": job.id,
                "status": job.status,
                "kind": job.kind,
                "attempts": job.attempts,
                "error": job.error,
                "result": job.result,
            }

    @app.get("/v1/catalog")
    def read_catalog(
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
        p: Principal = Depends(authenticate),
    ):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return organization.read(
                session,
                p.tenant_id,
                offset=offset,
                limit=limit,
                include_profile="profile:read" in p.scopes,
            )

    @app.post("/v1/catalog/organize", status_code=202)
    def organize_catalog(p: Principal = Depends(authenticate)):
        require(p, "knowledge:write")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            job = organization.enqueue(session, p.tenant_id, force=True)
            return {"job_id": job.id, "status": job.status}

    @app.patch("/v1/catalog")
    def edit_catalog(body: CatalogEdit, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return organization.edit(session, p.tenant_id, body)

    @app.patch("/v1/catalog/entries/{entry_id}")
    def edit_catalog_entry(entry_id: str, body: CatalogEntryEdit, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return organization.edit(session, p.tenant_id, body, entry_id)

    @app.post("/v1/knowledge/search")
    def knowledge_search(body: Search, p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return {"mode": "lexical", "items": services.search(session, body.query, body.limit)}

    @app.get("/v1/profile")
    def get_profile(p: Principal = Depends(authenticate)):
        require(p, "profile:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return services.profile(session, p.tenant_id)

    @app.post("/v1/claims/{claim_id}/decisions")
    def claim_decision(claim_id: str, body: DecisionInput, p: Principal = Depends(authenticate)):
        browser_only(p)
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.decide_claim(
                session, p.tenant_id, claim_id, body.action, body.expected_revision, body.value
            )
        return {"status": "saved"}

    @app.post("/v1/claims/proposals", status_code=201)
    def claim_proposals(body: Proposals, p: Principal = Depends(authenticate)):
        require(p, "knowledge:write")
        with db.session(p.tenant_id, actor=p.kind) as session:
            ids = services.propose_claims(
                session, p.tenant_id, body.source_id, body.expected_revision, body.items
            )
            return {"claim_ids": ids, "status": "candidate"}

    @app.get("/v1/pages")
    def pages(p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            return {"items": services.visible_pages(session)}

    @app.post("/v1/pages", status_code=201)
    def create_page(body: PageInput, p: Principal = Depends(authenticate)):
        require(p, "knowledge:write")
        with db.session(p.tenant_id, actor=p.kind) as session:
            page = services.save_page(session, p.tenant_id, body.title, body.content, body.evidence_ids,
                                      body.fact_ids, body.expected_revision)
            return {"id": page.id, "origin_kind": "generated"}

    @app.post("/v1/forms/drafts", status_code=201)
    def form_draft(body: FormInput, p: Principal = Depends(authenticate)):
        require(p, "forms:write")
        if len({f.key for f in body.fields}) != len(body.fields):
            raise HTTPException(422, "表单字段 key 不能重复")
        with db.session(p.tenant_id, actor=p.kind) as session:
            draft = services.prepare_form(session, p.tenant_id, body.title, body.fields)
            return services.draft_dict(session, p.tenant_id, draft)

    @app.get("/v1/forms/drafts/{draft_id}")
    def read_draft(draft_id: str, p: Principal = Depends(authenticate)):
        require(p, "forms:write")
        with db.session(p.tenant_id, actor=p.kind) as session:
            services.lock_tenant(session, p.tenant_id)
            draft = session.get(FormDraft, draft_id)
            if not draft:
                raise HTTPException(404, "草稿不存在")
            return services.draft_dict(session, p.tenant_id, draft)

    from akb.space_api import routes

    routes(app, db, settings)
    from akb.knowledge_api import routes as knowledge_routes

    knowledge_routes(app, db)
    from akb.files_api import routes as file_routes

    file_routes(app, db, settings)
    from akb.sync_api import routes as sync_routes

    sync_routes(app, db, settings)
    from akb.onboarding import routes as onboarding_routes

    onboarding_routes(app, db, settings)
    from akb.preview import routes as preview_routes

    preview_routes(app, settings)

    from akb.host_transfer import routes as host_transfer_routes
    host_transfer_routes(app, db, settings)
    if settings.assistant_public_origin:
        from akb.assistant_http import routes as assistant_routes

        assistant_routes(app, db, settings)
        from akb.guest_space import routes as guest_routes
        guest_routes(app, db, settings)
    if settings.knowledge_engine in {"tencent", "weknora"}:
        # The old business engine is retired, including its read endpoints. Retain
        # file/auth/onboarding infrastructure and archived tables for explicit rollback only.
        retired = ("/v1/knowledge", "/v1/catalog", "/v1/profile", "/v1/claims", "/v1/pages", "/v1/forms")
        app.router.routes = [route for route in app.router.routes
                             if not getattr(route, "path", "").startswith(retired)
                             and getattr(route, "path", "") != "/v1/search"]
        from akb.memory_api import routes as memory_routes

        memory_routes(app, db, settings)
        from akb.agent_jobs import routes as agent_job_routes

        agent_job_routes(app, db, settings)
        from fastapi.responses import RedirectResponse

        @app.get("/", include_in_schema=False)
        def library_home():
            return RedirectResponse(settings.native_origin + "/knowledge", status_code=307)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.native_origin],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Cloudreve-Token", "X-AKB-File-User"],
    )
    app.add_middleware(BodyLimitMiddleware, limit=settings.upload_limit + 100000)
    web_dist = ROOT / "web/dist"
    if settings.knowledge_engine == "legacy" and web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return app


def run():
    import uvicorn

    settings = Settings()
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.api_port)
