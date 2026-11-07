"""Remote MCP for the shared assistant. No endpoint opens the server's disk as a user's PC."""
import functools
from urllib.parse import urlsplit

import anyio
from fastapi import Depends, Request, Response
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, Field
from starlette._utils import get_route_path
from starlette.responses import JSONResponse

from akb import mcp_host, mcp_tencent, native, native_start
from akb import mcp_server as files
from akb.assistant_auth import AssistantAuthorization
from akb.assistant_package import package_version, published_package
from akb.schemas import Input
from akb.security import SCOPES

CLOUD_TOOLS = (
    "read_library", "search_library", "read_library_item", "save_library_item", "forget_library_item",
    "sync_library", "resolve_fact_evidence", "reparse_source", "prepare_filling", "validate_filling",
    "get_library_health", "read_onboarding", "start_onboarding", "update_onboarding", "list_sources",
    "read_source", "read_evidence", "import_text", "read_file_upload", "cancel_file_upload", "get_job",
    "get_storage_usage", "read_file_changes", "sync_file_changes",
)


class Approval(Input):
    reference: str = Field(min_length=30, max_length=100)
    nonce: str = Field(min_length=30, max_length=100)
    allowed: bool


class MCPPathAlias:
    """Serve the advertised resource URL directly, preserving auth and POST bodies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and get_route_path(scope) == "/mcp":
            # Starlette Mount otherwise redirects /mcp to /mcp/. Some clients
            # rebuild redirected requests without the original authorization.
            scope = {**scope, "path": scope["path"] + "/"}
            if "raw_path" in scope:
                scope["raw_path"] = scope["raw_path"] + b"/"
        await self.app(scope, receive, send)


def build_mcp(provider, settings):
    url = urlsplit(provider.origin)
    remote = FastMCP("Pagebeak", token_verifier=ProviderTokenVerifier(provider),
        auth=AuthSettings(issuer_url=provider.origin, resource_server_url=provider.resource,
                          required_scopes=["knowledge:read"], validate_token_resource=True),
        streamable_http_path="/", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[url.netloc], allowed_origins=[provider.origin]))

    def bind(function):
        @functools.wraps(function)
        async def invoke(*args, **kwargs):
            token = get_access_token()
            if not token or not token.subject or not token.claims or not token.claims.get("knowledge_base_id"):
                raise ValueError("当前连接未授权，请使用客户端的连接提示授权本人资料空间")
            marker = files.request_connection.set({"base": f"http://127.0.0.1:{settings.api_port}",
                "token": token.token, "expected": token.claims["knowledge_base_id"]})
            try:
                return await anyio.to_thread.run_sync(functools.partial(function, *args, **kwargs))
            finally:
                files.request_connection.reset(marker)
        return invoke

    for name in CLOUD_TOOLS + (("list_organization_jobs", "claim_organization_job", "renew_organization_job", "complete_organization_job", "release_organization_job") if settings.knowledge_engine == "weknora" else ()):
        function = getattr(mcp_tencent, name, None) or getattr(files, name)
        annotations = mcp_tencent.DELETE if name == "forget_library_item" else files.READ if name.startswith(
            ("read_", "get_", "search_", "resolve_", "validate_", "list_")) else files.WRITE
        remote.add_tool(bind(function), annotations=annotations)

    if settings.knowledge_engine == "weknora":
        for function in (mcp_host.prepare_host_file_upload, mcp_host.prepare_host_catalog_sync, mcp_host.read_shared_local_catalog):
            remote.add_tool(bind(function), annotations=files.READ if function.__name__.startswith("read_") else files.WRITE)

    @remote.tool(annotations=files.READ)
    @bind
    def get_knowledge_context(expected_knowledge_base_id: str | None = None,
                              expected_service_origin: str | None = None) -> dict:
        """先核对助理声明的服务地址，再核对登录用户及唯一绑定的知识库；地址或库不符必须停止。"""
        if expected_service_origin is not None and expected_service_origin.rstrip("/") != provider.origin:
            raise ValueError("任务指定服务地址与当前连接不符，停止操作；请从原生专家重新连接")
        data = files.get_knowledge_context(expected_knowledge_base_id)
        data.pop("knowledge_adoption", None)
        data.update({"contract": "weknora-workbuddy-v1-remote" if settings.knowledge_engine == "weknora" else "tencent-workbuddy-v5-remote", "transport": "streamableHttp",
                     "service_origin": provider.origin,
                     "trusted_skill_tool": "read_assistant_skill", "skill_package_version": package_version(settings.knowledge_engine),
                     "model_billing": "current_client_account", "background_model_api": False,
                     "authentication": {"owner": "pagebeak_user", "client_independent": True,
                                        "authorization": "oauth2.1_pkce"},
                     "connection_state": {"status": "authorized", "reauthorization_required": False,
                                          "next_action": "read_skill_and_resume_onboarding"},
                     "organization_jobs": {"available": settings.knowledge_engine == "weknora", "claim_tool": "claim_organization_job", "offline": "queued_until_workbuddy_runs"},
                     "local_documents": ({"available": True, "mode": "workbuddy_skill", "remote_path_access": False,
                         "requires_host_file_permission": True, "runtime_verified": False, "api_origin": provider.origin,
                         "reference": "references/local-documents.md", "upload_tool": "prepare_host_file_upload",
                         "note": "通过随助理安装的 Skill 脚本在教师电脑执行；先检查宿主运行环境，不额外安装客户端。"}
                         if settings.knowledge_engine == "weknora" else {"available": False, "reason": "当前 Tencent 远程包不含本机检索"}),
                     "file_upload": {"available": True, "mode": "file_workspace", "entry_tool": "get_upload_entry",
                                     "local_path_access": False, "upload_limit_bytes": settings.upload_limit}})
        return data

    @remote.tool(annotations=files.READ)
    def read_assistant_skill(reference: str = "SKILL.md") -> dict:
        """读取随服务固定的可信助理说明；仅允许列出的技能参考，不接受磁盘路径。"""
        from akb.assistant_package import skill_texts

        texts = skill_texts(settings.knowledge_engine, provider.origin)
        if reference not in texts:
            raise ValueError("未知技能参考。可用：" + ", ".join(sorted(texts)))
        return {"reference": reference, "text": texts[reference], "version": package_version(settings.knowledge_engine)}

    @remote.tool(annotations=files.READ)
    def get_upload_entry() -> dict:
        """返回本人的文件空间上传入口。远程服务无法读取教师电脑路径；原件上传后再等待解析并整理。"""
        return {"url": settings.native_origin + "/home", "upload_limit_bytes": settings.upload_limit,
                "method": "browser_upload", "message": "打开文件空间，选择或拖入原文件；已有本机连接可继续用本机上传工具。"}

    if settings.passwordless_enabled:
        @remote.tool(annotations=files.WRITE)
        @bind
        def open_my_files() -> dict:
            """用户要打开网页资料或连接新浏览器时，生成仅本人空间、两分钟内单次有效的打开链接。勿转发或写入记忆。"""
            return files.call("POST", "/v1/space/browser-link", {})

    return remote


def routes(app, db, settings):
    from fastapi import Header, HTTPException, Query

    if settings.knowledge_engine not in {"tencent", "weknora"}:
        raise ValueError("教师助理远程入口仅支持 Tencent Memory 或 WeKnora")
    provider = AssistantAuthorization(db, settings)
    mcp = build_mcp(provider, settings)
    registration = ClientRegistrationOptions(enabled=True, valid_scopes=sorted(SCOPES), default_scopes=sorted(SCOPES))
    auth_routes = create_auth_routes(provider, issuer_url=AnyHttpUrl(provider.origin),
        client_registration_options=registration, revocation_options=RevocationOptions(enabled=True))
    app.router.routes.extend(route for route in auth_routes if route.path != "/revoke")
    app.router.routes.extend(create_protected_resource_routes(resource_url=AnyHttpUrl(provider.resource),
        authorization_servers=[AnyHttpUrl(provider.origin)], scopes_supported=sorted(SCOPES)))
    app.mount("/mcp", mcp.streamable_http_app())
    app.add_middleware(MCPPathAlias)
    app.state.assistant_mcp = mcp
    app.state.assistant_authorization = provider

    @app.get('/v1/assistant/connection')
    def connection_info():
        # Connection metadata, not a promise that every host imports one manifest.
        return {'name': 'Pagebeak', 'url': provider.resource, 'transport': 'streamable_http',
                'authorization': 'oauth2.1_pkce', 'identity_required': settings.identity_required,
                'authorization_metadata': provider.origin + '/.well-known/oauth-authorization-server',
                'local_files_require_host_permission': True}

    @app.get("/v1/assistant/package")
    def shared_package():
        import hashlib

        try:
            content = published_package(settings)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(content, media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="zhixu-teacher-assistant.zip"',
            "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff",
            "ETag": '"' + hashlib.sha256(content).hexdigest() + '"'})

    @app.post("/revoke")
    async def revoke(request: Request):
        # SDK's current RevocationRequest requires client_secret even for public
        # clients. Keep SDK client authentication, accept RFC 7009's public form.
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
        try:
            client = await ClientAuthenticator(provider).authenticate_request(request)
        except AuthenticationError:
            return JSONResponse({"error": "unauthorized_client"}, status_code=401, headers=headers)
        form = await request.form()
        secret = form.get("token")
        if not isinstance(secret, str) or not 1 <= len(secret) <= 256:
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=headers)
        token = await provider.load_access_token(secret) or await provider.load_refresh_token(client, secret)
        if token and token.client_id == client.client_id:
            await provider.revoke_token(token)
        return Response(status_code=200, headers=headers)

    def owner(x_cloudreve_token: str = Header(min_length=20, max_length=10000),
              x_akb_file_user: str = Header(min_length=1, max_length=80)):
        identity = native.verify_identity(settings, x_cloudreve_token)
        if identity["id"] != x_akb_file_user:
            raise HTTPException(409, "文件账号已变化，请刷新后连接")
        return native_start.principal(db, identity)

    @app.get("/v1/native/assistant/authorization")
    def authorization(reference: str = Query(min_length=30, max_length=100), p=Depends(owner)):
        return provider.describe(reference, p)

    @app.post("/v1/native/assistant/authorization")
    def approve(body: Approval, p=Depends(owner)):
        return provider.approve(body.reference, body.nonce, p, allowed=body.allowed)
