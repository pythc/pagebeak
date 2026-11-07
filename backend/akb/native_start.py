"""First-use entry authenticated by the current drive identity, never an ambient KB cookie."""

import hashlib
import secrets
from datetime import timedelta
from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import select, text

from akb import native, services
from akb.models import Catalog, Credential, NativeConnection, Onboarding, Tenant, User, now, uid
from akb.schemas import Input
from akb.security import SCOPES, Principal, issue_credential, password_hash, password_matches

_DUMMY_HASH = password_hash(secrets.token_urlsafe(32))


class SetupInput(Input):
    mode: Literal["create", "link"] = "create"
    drive_password: str = Field(min_length=1, max_length=1000)
    email: str = Field(default="", max_length=254)
    knowledge_password: str = Field(default="", max_length=1000)


def linked_tenant(db, identity):
    with db.session() as session:
        return session.scalar(text("SELECT public.akb_native_tenant(:id)"), {"id": identity["id"]})


def principal(db, identity, issuing=False):
    tenant_id = linked_tenant(db, identity)
    if not tenant_id:
        raise HTTPException(409, "当前文件账号尚未启用知识库，请先在本页关联")
    with db.session(tenant_id) as session:
        services.lock_tenant(session, tenant_id)
        user = session.scalar(select(User).where(User.tenant_id == tenant_id))
        if not user:
            raise HTTPException(409, "知识库账号异常，请联系管理员")
        credential_id = ""
        if issuing:
            # This unexposed, short-lived issuer only backs the 15-minute installation ticket.
            # It is not a login cookie, a native refresh token, or a returned API credential.
            label = "native-start:" + hashlib.sha256(identity["id"].encode()).hexdigest()[:32]
            credential = session.scalar(select(Credential).where(
                Credential.user_id == user.id, Credential.kind == "browser", Credential.label == label,
                Credential.revoked.is_(False), Credential.expires_at > now() + timedelta(minutes=16),
            ))
            if not credential:
                _, credential = issue_credential(session, user.id, "browser", label)
                credential.expires_at = now() + timedelta(minutes=20)
            credential_id = credential.id
        return Principal(user.id, tenant_id, user.display_name, credential_id, set(SCOPES), "browser", "")


def setup(db, settings, identity, body):
    existing_tenant = linked_tenant(db, identity)
    with db.session() as session:
        existing_user = session.scalar(select(User).where(User.email == body.email.strip().casefold())) if body.mode == "link" else None
        if body.mode == "link":
            valid = password_matches(body.knowledge_password, existing_user.password_hash if existing_user else _DUMMY_HASH)
            if not existing_user or not valid:
                raise HTTPException(401, "资料库邮箱或密码不正确")
        if existing_tenant:
            if body.mode == "link" and existing_user.tenant_id != existing_tenant:
                raise HTTPException(409, "当前文件账号已关联其他资料库，不能覆盖")
            return
        target_tenant = existing_user.tenant_id if existing_user else None
    # Obtain an independent session, so background refresh cannot invalidate the browser session.
    try:
        with httpx.Client(base_url=settings.native_cloudreve_url, trust_env=False, timeout=20) as client:
            response = client.post('/api/v4/session/token', json={'email': identity['email'], 'password': body.drive_password})
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "文件服务暂不可达，请重试") from exc
    if result.get('code') != 0 or result.get('data', {}).get('user', {}).get('id') != identity['id']:
        raise HTTPException(401, "文件空间密码不正确，或该账号需要额外验证")
    token = result.get('data', {}).get('token')
    if not isinstance(token, dict) or any(not isinstance(token.get(key), str) or not token[key]
                                          for key in ['access_token', 'refresh_token', 'access_expires']):
        raise HTTPException(503, "文件服务没有返回完整的后台连接，请重试")
    saved_path = None
    try:
        with db.session(target_tenant) as session:
            if not target_tenant:
                target_tenant = uid()
                # Stable provider ID, not an email/name match, establishes ownership.
                synthetic_email = 'native.' + hashlib.sha256(identity['id'].encode()).hexdigest() + '@zhixu.invalid'
                session.add(User(email=synthetic_email, display_name=(identity.get('nickname') or identity.get('nick') or '个人资料库')[:80],
                                 password_hash=password_hash(secrets.token_urlsafe(32)), tenant_id=target_tenant))
                session.flush()
                session.execute(text("SELECT set_config('akb.tenant_id', :tenant, true)"), {'tenant': target_tenant})
                session.add(Tenant(id=target_tenant, quota_bytes=settings.default_quota_bytes))
                session.flush()
                session.add(Catalog(tenant_id=target_tenant))
                session.add(Onboarding(tenant_id=target_tenant))
            else:
                services.lock_tenant(session, target_tenant)
            if session.scalar(select(NativeConnection)):
                raise HTTPException(409, "此资料库已有文件空间绑定，不能直接覆盖")
            connection = NativeConnection(tenant_id=target_tenant, provider_user_id=identity['id'])
            session.add(connection)
            session.flush()  # Unique constraints serialize concurrent first-use/link attempts.
            saved_path = native.token_path(settings, connection.id)
            native.save_token(settings, connection.id, token)
            native.enqueue(session, target_tenant, 'frontend_observed')
    except BaseException:
        if saved_path:
            saved_path.unlink(missing_ok=True)
        raise
