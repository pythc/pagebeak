"""Passwordless personal spaces: a browser capability plus scoped native OAuth.

Provisioning is idempotent per device. Cloudreve's random service-side login is
never returned to the teacher or model; browser and worker get separate sessions.
"""
import hmac
import json
import secrets
from datetime import timedelta
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, Request, Response
from pydantic import Field
from sqlalchemy import func, select, text

from akb import native, native_start
from akb.models import (
    Catalog,
    Credential,
    ExternalIdentity,
    NativeConnection,
    Onboarding,
    SpaceDevice,
    SpaceLink,
    Tenant,
    User,
    now,
)
from akb.schemas import Input
from akb.security import SCOPES, Principal, authenticate, digest_token, password_hash, require
from akb.weknora_client import private_write

COOKIE = "zhixu_device"


def device_row(session, request, *, lock=False):
    token = request.cookies.get(COOKIE, '')
    if not 30 <= len(token) <= 100:
        raise HTTPException(401, '浏览器会话已失效，请刷新页面')
    query = select(SpaceDevice).where(SpaceDevice.digest == digest_token(token),
        SpaceDevice.revoked.is_(False), SpaceDevice.expires_at > now())
    row = session.scalar(query.with_for_update() if lock else query)
    if not row:
        raise HTTPException(401, '浏览器会话已失效，请刷新页面')
    return row


def guard_mutation(db, settings, request):
    if request.headers.get('origin') != settings.native_origin:
        raise HTTPException(403, '请从 Pagebeak 页面操作')
    with db.session() as session:
        row = device_row(session, request)
        if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), row.csrf):
            raise HTTPException(403, '页面已变化，请刷新后重试')


def create_personal_user(session, settings, tenant_id, *, prefix, display_name='我的资料'):
    user = User(tenant_id=tenant_id, email=prefix + '.' + tenant_id + '@zhixu.invalid',
                display_name=display_name[:80], password_hash=password_hash(secrets.token_urlsafe(32)))
    session.add(user)
    session.flush()
    session.execute(text("SELECT set_config('akb.tenant_id', :id, true)"), {'id': tenant_id})
    session.add(Tenant(id=tenant_id, quota_bytes=settings.default_quota_bytes))
    session.flush()
    session.add(Catalog(tenant_id=tenant_id))
    session.add(Onboarding(tenant_id=tenant_id))
    session.flush()
    return user


def ensure_identity_drive(db, settings, p):
    """Retry provisioning against the same durable user/tenant after a timeout."""
    with db.session(p.tenant_id) as session:
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {'key': 'identity-drive:' + p.tenant_id})
        connection = session.scalar(select(NativeConnection))
        if connection:
            if not connection.enabled:
                raise HTTPException(409, '资料空间连接已停用，请联系管理员；未创建替代空间')
            return
        managed = session.scalar(select(ExternalIdentity.id).where(
            ExternalIdentity.user_id == p.user_id, ExternalIdentity.provisions_space.is_(True)))
        if not managed:
            return  # Never replace a legacy drive by inferring ownership.
        drive = provision_drive(settings, p.tenant_id)
        connection = NativeConnection(tenant_id=p.tenant_id, provider_user_id=drive['user']['id'])
        session.add(connection)
        session.flush()
        native.save_token(settings, connection.id, drive['token'])
        native.enqueue(session, p.tenant_id, 'frontend_observed')


def managed_path(settings, tenant_id):
    return settings.managed_spaces_dir / (str(UUID(tenant_id)) + '.json')


def file_call(client, method, path, **kwargs):
    try:
        result = client.request(method, '/api/v4/' + path, **kwargs)
        result.raise_for_status()
        value = result.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, '资料空间暂不可达，请重试；无需重新安装助理') from exc
    if value.get('code') != 0:
        raise HTTPException(503, '资料空间尚未就绪，请重试或联系管理员')
    return value['data']


def managed_login(settings, identity):
    with httpx.Client(base_url=settings.native_cloudreve_url, timeout=20, trust_env=False) as client:
        data = file_call(client, 'POST', 'session/token', json={k: identity[k] for k in ('email', 'password')})
    token = data.get('token', {})
    if not data.get('user', {}).get('id') or not all(token.get(k) for k in ('access_token', 'refresh_token', 'access_expires', 'refresh_expires')):
        raise HTTPException(503, '资料空间会话不完整，请稍后重试')
    if identity.get('provider_id') and data['user']['id'] != identity['provider_id']:
        raise HTTPException(409, '资料空间绑定不一致，已停止连接')
    return data


def provision_drive(settings, tenant_id):
    path = managed_path(settings, tenant_id)
    if path.is_symlink():
        raise HTTPException(503, '资料空间配置无效')
    if not path.exists():
        private_write(path, {'email': 'space-' + tenant_id + '@zhixu.invalid',
                             'password': secrets.token_urlsafe(40), 'origin': settings.native_cloudreve_url})
    identity = json.loads(path.read_text())
    if identity['origin'] != settings.native_cloudreve_url:
        raise HTTPException(409, '文件服务地址已变化，需要管理员迁移绑定')
    # A durable pre-provision journal survives timeouts and transaction rollback.
    try:
        login = managed_login(settings, identity)
    except HTTPException:
        if identity.get('provider_id'):
            raise  # Never recreate/reset a previously bound account after revocation.
        if not settings.provisioner_file:
            raise HTTPException(503, '管理员尚未启用自动开通资料空间') from None
        admin = json.loads(settings.provisioner_file.read_text())
        with httpx.Client(base_url=settings.native_cloudreve_url, timeout=20, trust_env=False) as client:
            admin_session = file_call(client, 'POST', 'session/token', json={k: admin[k] for k in ('email', 'password')})
            client.headers['Authorization'] = 'Bearer ' + admin_session['token']['access_token']
            file_call(client, 'PUT', 'admin/user', json={'user': {'id': 0, 'email': identity['email'],
                'nick': '我的资料', 'status': 'active', 'group_users': settings.managed_group_id, 'edges': {}},
                'password': identity['password']})
        login = managed_login(settings, identity)
    identity['provider_id'] = login['user']['id']
    private_write(path, identity)
    return login


def principal(session, device):
    user = session.get(User, device.user_id) if device.user_id else None
    if not user:
        raise HTTPException(409, '个人空间尚未开通')
    return Principal(user.id, user.tenant_id, user.display_name, '', set(SCOPES), 'browser', device.csrf)


class LinkInput(Input):
    code: str = Field(min_length=30, max_length=100)


def routes(app, db, settings):
    if not settings.passwordless_enabled:
        return
    from akb.assistant_http import Approval
    from akb.assistant_package import installation
    provider = app.state.assistant_authorization

    def cookie(response, value):
        response.set_cookie(COOKIE, value, httponly=True, secure=settings.cookie_secure,
                            samesite='lax', path='/', max_age=90 * 86400)

    def mutation(request: Request):
        guard_mutation(db, settings, request)

    from akb.identity import IdentityService
    from akb.identity import routes as identity_routes
    identities = IdentityService(db, settings)
    identity_routes(app, identities, mutation)

    @app.get('/v1/welcome')
    def welcome(request: Request, response: Response, reference: str | None = None):
        with db.session() as session:
            try:
                row = device_row(session, request)
            except HTTPException:
                token = secrets.token_urlsafe(32)
                row = SpaceDevice(digest=digest_token(token), csrf=secrets.token_urlsafe(32),
                                  expires_at=now() + timedelta(days=90))
                session.add(row)
                cookie(response, token)
            bound_id = principal(session, row).tenant_id if row.user_id else None
            # An ordinary MCP/OIDC deployment does not need a WorkBuddy package
            # or publisher metadata. That distribution is an optional adapter.
            package = installation(settings, bound_id) if settings.assistant_author_email else None
            state = {'csrf': row.csrf, 'has_space': bool(row.user_id), 'installation': package}
            state['identity'] = identities.describe(session, row.user_id)
            if reference:
                grant = provider.pending(session, reference)
                from akb.models import AssistantClient
                client = session.get(AssistantClient, grant.client_id)
                state['consent'] = {'client_name': (client.metadata_json.get('client_name') or 'Agent 客户端')[:120],
                                    'scopes': grant.parameters['scopes']}
            return state

    def enter(request):
        with db.session() as session:
            row = device_row(session, request, lock=True)
            if row.user_id:
                p = principal(session, row)
                if request.headers.get('x-cloudreve-token'):
                    identity = native.verify_identity(settings, request.headers['x-cloudreve-token'])
                    if native_start.linked_tenant(db, identity) != p.tenant_id:
                        raise HTTPException(409, '浏览器文件空间与设备空间不同，已停止；请使用独立浏览器配置')
                return p
            # An existing *verified* drive binding may be adopted, never merged by name/email.
            old_token = request.headers.get('x-cloudreve-token')
            if old_token:
                identity = native.verify_identity(settings, old_token)
                existing = native_start.linked_tenant(db, identity)
                if not existing:
                    raise HTTPException(409, '旧文件空间尚未迁移；原资料已保留，请联系管理员迁移后继续')
                user = session.scalar(select(User).where(User.tenant_id == existing))
                row.user_id = user.id
                return principal(session, row)
            if settings.identity_required:
                raise HTTPException(401, '请先使用已有账号授权，系统将自动建立个人空间')
            session.execute(text("SELECT pg_advisory_xact_lock(73103125)"))
            if session.scalar(select(func.count()).select_from(User).where(User.email.like('device.%@zhixu.invalid'))) >= settings.auto_space_limit:
                raise HTTPException(503, '当前可开通空间已满，请联系管理员扩容')
            # Device ID is stable even when an external call times out before DB commit.
            tenant_id = row.id
            drive = provision_drive(settings, tenant_id)
            user = create_personal_user(session, settings, tenant_id, prefix='device')
            connection = NativeConnection(tenant_id=tenant_id, provider_user_id=drive['user']['id'])
            session.add(connection)
            session.flush()
            native.save_token(settings, connection.id, drive['token'])
            native.enqueue(session, tenant_id, 'frontend_observed')
            row.user_id = user.id
            return principal(session, row)

    @app.post('/v1/welcome/space', dependencies=[Depends(mutation)])
    def create_space(request: Request):
        p = enter(request)
        ensure_identity_drive(db, settings, p)
        return {'knowledge_base_id': p.tenant_id, 'display_name': p.display_name}

    @app.post('/v1/welcome/session', dependencies=[Depends(mutation)])
    def file_session(request: Request):
        with db.session() as session:
            p = principal(session, device_row(session, request))
        token = request.headers.get('x-cloudreve-token', '')
        if token:
            identity = native.verify_identity(settings, token)
            if native_start.linked_tenant(db, identity) != p.tenant_id:
                raise HTTPException(409, '浏览器已有不同资料空间，未替换；请使用独立浏览器配置')
        ensure_identity_drive(db, settings, p)
        path = managed_path(settings, p.tenant_id)
        if not path.exists():
            if token:
                return {'existing': True}
            raise HTTPException(409, '原文件空间会话需要管理员迁移；不会新建或替换资料')
        return managed_login(settings, json.loads(path.read_text()))

    @app.get('/v1/welcome/authorization')
    def describe(request: Request, reference: str):
        with db.session() as session:
            p = principal(session, device_row(session, request))
        return provider.describe(reference, p)

    @app.post('/v1/welcome/authorization', dependencies=[Depends(mutation)])
    def approve(request: Request, body: Approval):
        with db.session() as session:
            p = principal(session, device_row(session, request))
        return provider.approve(body.reference, body.nonce, p, allowed=body.allowed)

    @app.post('/v1/space/browser-link')
    def browser_link(p=Depends(authenticate)):
        for scope in SCOPES:
            require(p, scope)
        if p.kind != 'api':
            raise HTTPException(403, '需要已连接的助理')
        if not managed_path(settings, p.tenant_id).exists():
            return {'url': settings.native_origin + '/home', 'expires_in': None, 'legacy': True}
        code = secrets.token_urlsafe(32)
        with db.session() as session:
            session.add(SpaceLink(digest=digest_token(code), user_id=p.user_id,
                                 credential_id=p.credential_id, expires_at=now() + timedelta(minutes=2)))
        return {'url': settings.native_origin + '/start#space=' + code, 'expires_in': 120,
                'note': '本人浏览器一次性打开，勿转发；过期请重新获取'}

    @app.post('/v1/welcome/restore', dependencies=[Depends(mutation)])
    def restore(request: Request, body: LinkInput):
        with db.session() as session:
            row = device_row(session, request, lock=True)
            link = session.scalar(select(SpaceLink).where(SpaceLink.digest == digest_token(body.code)).with_for_update())
            credential = session.get(Credential, link.credential_id) if link else None
            if not link or link.used_at or link.expires_at <= now() or not credential or credential.revoked or credential.expires_at <= now():
                raise HTTPException(410, '打开链接已过期，请让助理重新打开我的资料')
            if row.user_id and row.user_id != link.user_id:
                raise HTTPException(409, '此浏览器已有其他资料空间；请使用独立浏览器配置，未切换或合并资料')
            token = request.headers.get('x-cloudreve-token')
            if token:
                identity = native.verify_identity(settings, token)
                user = session.get(User, link.user_id)
                if native_start.linked_tenant(db, identity) != user.tenant_id:
                    raise HTTPException(409, '浏览器已有不同资料空间，未切换；请使用独立浏览器配置')
            row.user_id, link.used_at = link.user_id, now()
            return {'restored': True}
