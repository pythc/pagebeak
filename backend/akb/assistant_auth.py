"""Persisted OAuth 2.1 authorization for HTTP MCP clients.

The SDK validates client redirects and S256 PKCE. This module binds an explicit
file-space consent to a user; the model never receives file-account passwords.
"""
import hmac
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from sqlalchemy import select

from akb.models import AssistantClient, AssistantGrant, Credential, ExternalIdentity, User, now
from akb.security import SCOPES, digest_token, issue_credential


def public_origin(value):
    url = urlsplit(value)
    if (url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}
            or not url.hostname or (url.scheme != "https" and not (
                url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}))):
        raise ValueError("助理公开地址必须是 HTTPS 根地址；HTTP 仅允许本机验收")
    return str(AnyHttpUrl(value)).rstrip("/")


class AssistantAuthorization:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.origin = public_origin(settings.assistant_public_origin)
        self.resource = self.origin + "/mcp"

    def require_identity(self, session, user_id):
        if self.settings.identity_required and not session.scalar(select(ExternalIdentity.id).where(
                ExternalIdentity.user_id == user_id)):
            raise HTTPException(401, '请先验证并绑定已有账号，再授权客户端连接当前资料空间')

    async def get_client(self, client_id):
        with self.db.session() as session:
            row = session.get(AssistantClient, client_id)
            return OAuthClientInformationFull.model_validate(row.metadata_json) if row else None

    async def register_client(self, client_info):
        if (client_info.token_endpoint_auth_method != "none" or not client_info.client_id
                or len(client_info.client_id) > 200 or len(client_info.redirect_uris or []) > 10):
            raise RegistrationError("invalid_client_metadata", "使用不含客户端密钥的公共客户端")
        for uri in client_info.redirect_uris or []:
            target = urlsplit(str(uri))
            if (target.username or target.password or target.fragment or not (
                target.scheme == "https" and target.hostname
                or target.scheme in self.settings.assistant_redirect_schemes
                or re.fullmatch(r"[a-z][a-z0-9+-]*(?:\.[a-z0-9+-]+)+", target.scheme)
                or target.scheme == "http" and target.hostname in {"localhost", "127.0.0.1", "::1"}
            )):
                raise RegistrationError("invalid_redirect_uri", "回调需使用 HTTPS、本机回环或已支持的原生应用协议")
        with self.db.session() as session:
            session.add(AssistantClient(id=client_info.client_id, metadata_json=client_info.model_dump(mode="json")))

    async def authorize(self, client, params):
        if params.resource not in {None, self.resource}:
            raise AuthorizeError("invalid_request", "授权目标不是当前知序服务")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "需要有效的 S256 PKCE challenge")
        scopes = params.scopes or sorted(SCOPES)
        if not set(scopes) <= SCOPES or "knowledge:read" not in scopes:
            raise AuthorizeError("invalid_scope", "授权范围无效")
        reference = secrets.token_urlsafe(32)
        parameters = params.model_copy(update={"scopes": scopes, "resource": self.resource}).model_dump(mode="json")
        with self.db.session() as session:
            session.add(AssistantGrant(kind="pending", digest=digest_token(reference), client_id=client.client_id,
                                       parameters=parameters, expires_at=now() + timedelta(minutes=10)))
        return self.settings.native_origin + "/start?" + urlencode({"assistant_request": reference})

    def pending(self, session, reference, *, lock=False):
        if not isinstance(reference, str) or len(reference) > 100:
            raise HTTPException(400, "连接请求无效")
        query = select(AssistantGrant).where(AssistantGrant.kind == "pending",
            AssistantGrant.digest == digest_token(reference))
        row = session.scalar(query.with_for_update() if lock else query)
        if not row or row.used_at or row.expires_at <= now() or row.parameters.get("resource") != self.resource:
            raise HTTPException(410, "连接请求已过期或已处理，请从当前客户端重新连接")
        return row

    def describe(self, reference, p):
        with self.db.session() as session:
            self.require_identity(session, p.user_id)
            row = self.pending(session, reference, lock=True)
            client = session.get(AssistantClient, row.client_id)
            nonce = secrets.token_urlsafe(32)
            row.parameters = {**row.parameters, "approval_nonce": digest_token(nonce), "approval_user": p.user_id}
            return {"client_name": (client.metadata_json.get("client_name") or "Agent 客户端")[:120],
                    "knowledge_base_id": p.tenant_id, "display_name": p.display_name,
                    "scopes": row.parameters["scopes"], "nonce": nonce,
                    "expires_at": row.expires_at, "model_billing": "由当前客户端使用的模型账号承担"}

    def approve(self, reference, nonce, p, *, allowed):
        with self.db.session() as session:
            self.require_identity(session, p.user_id)
            row = self.pending(session, reference, lock=True)
            params = row.parameters
            if (params.get("approval_user") != p.user_id or not isinstance(nonce, str) or len(nonce) > 100
                    or not hmac.compare_digest(params.get("approval_nonce", ""), digest_token(nonce))):
                raise HTTPException(409, "账号或连接页面已变化，请重新检查后授权")
            row.used_at = now()
            if not allowed:
                return {"redirect_url": construct_redirect_uri(params["redirect_uri"],
                    error="access_denied", state=params.get("state"))}
            code = secrets.token_urlsafe(32)
            clean = {k: v for k, v in params.items() if not k.startswith("approval_")}
            session.add(AssistantGrant(kind="code", digest=digest_token(code), client_id=row.client_id,
                                       user_id=p.user_id, parameters=clean,
                                       expires_at=now() + timedelta(minutes=5)))
            return {"redirect_url": construct_redirect_uri(params["redirect_uri"], code=code, state=params.get("state"))}

    async def load_authorization_code(self, client, authorization_code):
        with self.db.session() as session:
            row = session.scalar(select(AssistantGrant).where(AssistantGrant.kind == "code",
                AssistantGrant.digest == digest_token(authorization_code), AssistantGrant.client_id == client.client_id))
            if not row or row.used_at or row.expires_at <= now() or row.parameters.get("resource") != self.resource:
                return None
            params = AuthorizationParams.model_validate(row.parameters)
            return AuthorizationCode(code=authorization_code, client_id=row.client_id,
                scopes=params.scopes or [], expires_at=row.expires_at.timestamp(), code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource, subject=row.user_id)

    def tokens(self, session, client_id, user_id, scopes, credential=None):
        if credential is None:
            token, credential = issue_credential(session, user_id, "api", "Pagebeak MCP", scopes)
        else:
            token = "akb_" + secrets.token_urlsafe(32)
            credential.digest, credential.scopes = digest_token(token), scopes
        credential.expires_at = now() + timedelta(hours=1)
        refresh = "zxr_" + secrets.token_urlsafe(32)
        session.add(AssistantGrant(kind="refresh", digest=digest_token(refresh), client_id=client_id,
            user_id=user_id, credential_id=credential.id, parameters={"scopes": scopes, "resource": self.resource},
            expires_at=now() + timedelta(days=30)))
        return OAuthToken(access_token=token, token_type="Bearer", expires_in=3600,
                          refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        with self.db.session() as session:
            row = session.scalar(select(AssistantGrant).where(AssistantGrant.kind == "code",
                AssistantGrant.digest == digest_token(authorization_code.code)).with_for_update())
            if not row or row.used_at or row.expires_at <= now() or row.client_id != client.client_id or row.parameters.get("resource") != self.resource:
                raise TokenError("invalid_grant", "授权码已失效")
            row.used_at = now()
            return self.tokens(session, row.client_id, row.user_id, row.parameters["scopes"])

    async def load_refresh_token(self, client, refresh_token):
        with self.db.session() as session:
            row = session.scalar(select(AssistantGrant).where(AssistantGrant.kind == "refresh",
                AssistantGrant.digest == digest_token(refresh_token), AssistantGrant.client_id == client.client_id))
            credential = session.get(Credential, row.credential_id) if row else None
            if not row or row.used_at or row.expires_at <= now() or not credential or credential.revoked or row.parameters.get("resource") != self.resource:
                return None
            return RefreshToken(token=refresh_token, client_id=row.client_id, scopes=row.parameters["scopes"],
                expires_at=int(row.expires_at.timestamp()), resource=self.resource, subject=row.user_id)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        with self.db.session() as session:
            row = session.scalar(select(AssistantGrant).where(AssistantGrant.kind == "refresh",
                AssistantGrant.digest == digest_token(refresh_token.token)).with_for_update())
            credential = session.get(Credential, row.credential_id, with_for_update=True) if row else None
            if (not row or row.client_id != client.client_id or row.used_at or row.expires_at <= now()
                    or row.parameters.get("resource") != self.resource or not credential or credential.revoked or not set(scopes) <= set(row.parameters["scopes"])):
                raise TokenError("invalid_grant", "刷新授权已失效")
            row.used_at = now()
            # Preserve the credential ID so checkpoints and onboarding survive refresh.
            return self.tokens(session, row.client_id, row.user_id, scopes, credential)

    async def load_access_token(self, token):
        with self.db.session() as session:
            credential = session.scalar(select(Credential).where(Credential.digest == digest_token(token),
                Credential.kind == "api", Credential.revoked.is_(False), Credential.expires_at > now()))
            if not credential:
                return None
            row = session.scalar(select(AssistantGrant).where(AssistantGrant.kind == "refresh",
                AssistantGrant.credential_id == credential.id, AssistantGrant.used_at.is_(None),
                AssistantGrant.expires_at > now()))
            if not row or row.parameters.get("resource") != self.resource:
                return None
            user = session.get(User, credential.user_id)
            return AccessToken(token=token, client_id=row.client_id, scopes=credential.scopes,
                expires_at=int(credential.expires_at.timestamp()), resource=self.resource, subject=user.id,
                claims={"knowledge_base_id": user.tenant_id})

    async def revoke_token(self, token):
        with self.db.session() as session:
            if isinstance(token, AccessToken):
                credential = session.scalar(select(Credential).where(Credential.digest == digest_token(token.token)))
            else:
                row = session.scalar(select(AssistantGrant).where(AssistantGrant.kind == "refresh",
                    AssistantGrant.digest == digest_token(token.token)))
                credential = session.get(Credential, row.credential_id) if row else None
            if credential:
                credential.revoked = True
