"""Federated identity independent of browser sessions and MCP client brands.

Only configured authorization servers establish ownership. The server exchanges
one-time codes; upstream bearer tokens and profile emails are never identities.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta
from typing import Literal
from urllib.parse import quote, urlencode, urlsplit

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import func, select, text

from akb.models import (
    AssistantClient,
    AssistantGrant,
    Credential,
    ExternalIdentity,
    IdentityLogin,
    SpaceDevice,
    User,
    now,
    uid,
)
from akb.schemas import Input
from akb.security import digest_token


class IdentityProvider(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(pattern=r'^[a-z][a-z0-9-]{0,79}$')
    label: str = Field(min_length=1, max_length=80)
    kind: Literal['oidc', 'workbuddy'] = 'oidc'
    issuer: str = Field(max_length=512)
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: SecretStr = SecretStr('')
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str | None = None
    token_auth: Literal['client_secret_basic', 'client_secret_post', 'none'] = 'client_secret_basic'
    scopes: list[str] = Field(default_factory=lambda: ['openid', 'profile'], min_length=1, max_length=10)

    @model_validator(mode='after')
    def validate_trust(self):
        for value in (self.issuer, self.authorization_endpoint, self.token_endpoint, self.jwks_uri):
            if value is None:
                continue
            url = urlsplit(value)
            if (url.scheme != 'https' or not url.hostname or url.username or url.password
                    or url.fragment or url.query):
                raise ValueError('身份提供方端点必须是固定的 HTTPS 地址，不含查询参数或凭据')
        if self.token_auth != 'none' and not self.client_secret.get_secret_value():
            raise ValueError('身份提供方缺少服务端应用凭据')
        if self.kind == 'oidc' and (not self.jwks_uri or 'openid' not in self.scopes):
            raise ValueError('OIDC 需要 JWKS 地址和 openid scope')
        if any(not re.fullmatch(r'[A-Za-z0-9._:-]+', scope) for scope in self.scopes):
            raise ValueError('身份授权范围格式无效')
        if self.kind == 'workbuddy':
            expected = ('https://www.workbuddy.cn', 'https://www.workbuddy.cn/openapi/v2/authorize',
                        'https://www.workbuddy.cn/openapi/v2/token')
            if (self.issuer, self.authorization_endpoint, self.token_endpoint) != expected:
                raise ValueError('WorkBuddy 适配器只连接官方身份端点')
            if self.token_auth != 'client_secret_post' or self.scopes != ['user.profile.readable']:
                raise ValueError('WorkBuddy 使用服务端应用凭据，仅请求基础资料权限')
        return self

    def fingerprint(self):
        value = self.model_dump(mode='json')
        value['client_secret'] = self.client_secret.get_secret_value()
        return digest_token(json.dumps(value, sort_keys=True))

    @property
    def namespace(self):
        # WorkBuddy open_id is never assumed to be global across developer apps.
        return self.client_id if self.kind == 'workbuddy' else ''


class LoginInput(Input):
    provider: str = Field(min_length=1, max_length=80)
    intent: Literal['signin', 'link'] = 'signin'
    reference: str | None = Field(default=None, min_length=30, max_length=100)


class IdentityService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.providers = {}
        if settings.identity_providers_file:
            try:
                path = settings.identity_providers_file
                if path.is_symlink() or path.stat().st_size > 65536:
                    raise ValueError()
                values = json.loads(path.read_text())
                if not isinstance(values, list) or not 1 <= len(values) <= 10:
                    raise ValueError()
                for value in values:
                    provider = IdentityProvider.model_validate(value)
                    if provider.key in self.providers:
                        raise ValueError()
                    self.providers[provider.key] = provider
            except (OSError, ValueError):
                raise ValueError('外部身份配置无效；检查私有配置文件，未启动身份接入') from None
        if settings.identity_required and not self.providers:
            raise ValueError('启用账号身份前须配置身份提供方；不会退回匿名创建空间')
        if self.providers:
            from akb.assistant_auth import public_origin
            public_origin(settings.native_origin)
            if urlsplit(settings.native_origin).scheme == 'https' and not settings.cookie_secure:
                raise ValueError('HTTPS 账号接入必须启用 Secure 会话 Cookie')
        self.callback = settings.native_origin.rstrip('/') + '/v1/welcome/identity/callback'

    def http_client(self):
        return httpx.Client(timeout=10, follow_redirects=False, trust_env=False)

    def describe(self, session, user_id):
        verified = bool(user_id and session.scalar(select(ExternalIdentity.id).where(
            ExternalIdentity.user_id == user_id)))
        return {'required': self.settings.identity_required, 'verified': verified,
                'providers': [{'key': p.key, 'label': p.label} for p in self.providers.values()],
                'browser_is_identity': not (verified or self.settings.identity_required)}

    @staticmethod
    def _json(client, method, url, **kwargs):
        try:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            if len(response.content) > 262144:
                raise ValueError()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, '账号服务暂不可用或返回无效，请重新发起授权；未改动资料归属') from None

    def exchange(self, provider, code, transaction):
        data = {'grant_type': 'authorization_code', 'code': code, 'client_id': provider.client_id,
                'redirect_uri': self.callback}
        if provider.kind == 'oidc':
            data['code_verifier'] = transaction.verifier
        auth = None
        if provider.token_auth == 'client_secret_post':
            data['client_secret'] = provider.client_secret.get_secret_value()
        elif provider.token_auth == 'client_secret_basic':
            auth = httpx.BasicAuth(quote(provider.client_id, safe=''), quote(provider.client_secret.get_secret_value(), safe=''))
        with self.http_client() as client:
            result = self._json(client, 'POST', provider.token_endpoint, data=data, auth=auth)
            if provider.kind == 'workbuddy':
                # This response is obtained directly from the fixed token endpoint
                # with our application secret, never supplied by the client/model.
                subject = result.get('open_id')
                kind, access = result.get('token_type'), result.get('access_token')
                if not isinstance(kind, str) or kind.casefold() != 'bearer' or not isinstance(access, str) or not access:
                    raise HTTPException(401, '账号服务未返回有效的用户授权')
                return self._subject(subject)
            token = result.get('id_token')
            try:
                if not isinstance(token, str) or len(token) > 20000:
                    raise ValueError()
                header = jwt.get_unverified_header(token)
                if header.get('alg') not in {'RS256', 'ES256'}:
                    raise ValueError()
                keys = self._json(client, 'GET', provider.jwks_uri).get('keys', [])
                if not isinstance(keys, list) or not isinstance(header.get('kid'), str):
                    raise ValueError()
                matching = [key for key in keys if isinstance(key, dict)
                            and key.get('kid') == header.get('kid')
                            and key.get('use', 'sig') == 'sig'
                            and ('key_ops' not in key or 'verify' in key['key_ops'])
                            and key.get('alg', header['alg']) == header['alg']]
                if len(matching) != 1:
                    raise ValueError()
                key = jwt.PyJWK.from_dict(matching[0], algorithm=header['alg'])
                claims = jwt.decode(token, key.key, algorithms=[header['alg']], issuer=provider.issuer,
                    audience=provider.client_id, leeway=10,
                    options={'require': ['iss', 'sub', 'aud', 'exp', 'iat', 'nonce']})
                if (not isinstance(claims['nonce'], str) or not hmac.compare_digest(
                        digest_token(claims['nonce']), transaction.nonce_digest)):
                    raise ValueError()
                aud = claims['aud']
                if (isinstance(aud, list) and len(aud) > 1 or 'azp' in claims) and claims.get('azp') != provider.client_id:
                    raise ValueError()
                if claims.get('at_hash'):
                    access = result.get('access_token')
                    if not isinstance(access, str):
                        raise ValueError()
                    expected = base64.urlsafe_b64encode(hashlib.sha256(access.encode('ascii')).digest()[:16]).rstrip(b'=').decode()
                    if not hmac.compare_digest(expected, claims['at_hash']):
                        raise ValueError()
                return self._subject(claims['sub'])
            except (jwt.PyJWTError, TypeError, ValueError, KeyError):
                raise HTTPException(401, '账号身份校验失败；未建立或合并资料空间') from None

    @staticmethod
    def _subject(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 255 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise HTTPException(401, '账号服务未返回有效的稳定用户标识')
        return value

    def begin(self, request, body):
        from akb.guest_space import device_row
        provider = self.providers.get(body.provider)
        if not provider:
            raise HTTPException(404, '此账号登录方式尚未配置')
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        with self.db.session() as session:
            device = device_row(session, request, lock=True)
            if body.intent == 'link' and not device.user_id:
                raise HTTPException(409, '绑定已有空间需要先证明该空间的访问权')
            if body.reference:
                request.app.state.assistant_authorization.pending(session, body.reference)
            session.add(IdentityLogin(digest=digest_token(state), provider=provider.key,
                configuration=provider.fingerprint(), device_id=device.id, intent=body.intent,
                user_id=device.user_id, nonce_digest=digest_token(nonce), verifier=verifier,
                reference=body.reference, expires_at=now() + timedelta(minutes=10)))
        params = {'client_id': provider.client_id, 'redirect_uri': self.callback, 'response_type': 'code',
                  'scope': ' '.join(provider.scopes), 'state': state}
        if provider.kind == 'oidc':
            params.update(nonce=nonce, code_challenge=base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode(), code_challenge_method='S256')
        return {'authorization_url': provider.authorization_endpoint + '?' + urlencode(params)}

    def finish(self, request, state, code, error):
        from akb.guest_space import create_personal_user, device_row
        # Commit consumption even when exchange, signature checks or provisioning
        # fail. A failed callback cannot be replayed to consume another response.
        with self.db.session() as session:
            device = device_row(session, request, lock=True)
            transaction = session.scalar(select(IdentityLogin).where(
                IdentityLogin.digest == digest_token(state)).with_for_update())
            if (not transaction or transaction.used_at or transaction.expires_at <= now()
                    or transaction.device_id != device.id or transaction.user_id != device.user_id):
                raise HTTPException(410, '账号授权已失效或浏览器账号已变化，请重新连接')
            transaction.used_at = now()
            provider = self.providers.get(transaction.provider)
            if not provider or transaction.configuration != provider.fingerprint():
                raise HTTPException(409, '账号配置已变化，请重新连接')
        if error:
            raise HTTPException(400, '账号授权未完成，资料空间未改变')
        if not code or len(code) > 2048:
            raise HTTPException(400, '缺少有效的账号授权码')
        subject = self.exchange(provider, code, transaction)
        with self.db.session() as session:
            device = device_row(session, request, lock=True)
            if device.id != transaction.device_id or device.user_id != transaction.user_id:
                raise HTTPException(409, '账号已变化，请重新连接')
            # Serialize both duplicate logins and different accounts creating a
            # space against the same allocation cap. Auth metadata is not tenant data.
            session.execute(text('SELECT pg_advisory_xact_lock(73103125)'))
            identity = session.scalar(select(ExternalIdentity).where(
                ExternalIdentity.issuer == provider.issuer, ExternalIdentity.namespace == provider.namespace,
                ExternalIdentity.subject == subject))
            new_space = False
            if transaction.intent == 'link':
                if identity and identity.user_id != transaction.user_id:
                    raise HTTPException(409, '此账号已绑定其他空间，未合并或替换资料')
                user = session.get(User, transaction.user_id)
            elif identity:
                user = session.get(User, identity.user_id)
                if device.user_id and device.user_id != user.id:
                    raise HTTPException(409, '当前空间与登录账号不同，请先退出；未切换或合并资料')
            else:
                if device.user_id:
                    raise HTTPException(409, '当前已有资料，请明确选择绑定当前空间；未新建或覆盖资料')
                count = session.scalar(select(func.count()).select_from(User).where(
                    User.email.like('device.%@zhixu.invalid') | User.email.like('identity.%@zhixu.invalid')))
                if count >= self.settings.auto_space_limit:
                    raise HTTPException(503, '当前可开通空间已满，请联系管理员扩容')
                user = create_personal_user(session, self.settings, uid(), prefix='identity')
                new_space = True
            if not identity:
                session.add(ExternalIdentity(issuer=provider.issuer, namespace=provider.namespace,
                    subject=subject, user_id=user.id, provisions_space=new_space))
            else:
                identity.last_login_at = now()
            device.user_id = user.id
            # Invalidate the pre-login cookie, not just its CSRF secret: it must
            # never become a reusable authenticated session after fixation.
            cookie = secrets.token_urlsafe(32)
            session.add(SpaceDevice(digest=digest_token(cookie), user_id=user.id,
                csrf=secrets.token_urlsafe(32), expires_at=now() + timedelta(days=90)))
            device.revoked = True
        params = {'identity': 'connected'}
        if transaction.reference:
            params['assistant_request'] = transaction.reference
        return self.settings.native_origin + '/start?' + urlencode(params), cookie


def routes(app, service, mutation):
    from akb.guest_space import COOKIE, device_row

    app.state.identity_service = service

    @app.post('/v1/welcome/identity/start', dependencies=[Depends(mutation)])
    def begin(request: Request, body: LoginInput):
        return service.begin(request, body)

    @app.get('/v1/welcome/identity/callback')
    def callback(request: Request):
        query = request.query_params
        if (len(query.getlist('state')) != 1 or len(query.getlist('code')) > 1
                or len(query.getlist('error')) > 1 or not 30 <= len(query.get('state', '')) <= 100):
            raise HTTPException(400, '账号回调参数无效')
        target, cookie = service.finish(request, query['state'], query.get('code'), query.get('error'))
        response = RedirectResponse(target, status_code=303, headers={'Referrer-Policy': 'no-referrer'})
        response.set_cookie(COOKIE, cookie, httponly=True, secure=service.settings.cookie_secure,
                            samesite='lax', path='/', max_age=90 * 86400)
        response.delete_cookie('akb_session', path='/')
        return response

    @app.post('/v1/welcome/identity/logout', dependencies=[Depends(mutation)])
    def logout(request: Request, response: Response):
        with service.db.session() as session:
            device = device_row(session, request, lock=True)
            device.revoked = True
        response.delete_cookie(COOKIE, path='/')
        response.delete_cookie('akb_session', path='/')
        # Does not revoke independent MCP client grants or delete native files.
        return {'signed_out': True}

    @app.get('/v1/welcome/connections')
    def connections(request: Request):
        with service.db.session() as session:
            device = device_row(session, request)
            if not device.user_id:
                raise HTTPException(401, '请先完成账号授权')
            records = session.execute(select(Credential, AssistantGrant, AssistantClient).join(
                AssistantGrant, AssistantGrant.credential_id == Credential.id).join(
                AssistantClient, AssistantClient.id == AssistantGrant.client_id).where(
                Credential.user_id == device.user_id, Credential.revoked.is_(False),
                AssistantGrant.kind == 'refresh', AssistantGrant.used_at.is_(None),
                AssistantGrant.expires_at > now())).all()
            return [{'id': c.id, 'client_name': a.metadata_json.get('client_name') or 'Agent 客户端',
                     'scopes': c.scopes, 'expires_at': grant.expires_at} for c, grant, a in records]

    @app.post('/v1/welcome/connections/{credential_id}/revoke', dependencies=[Depends(mutation)])
    def revoke(request: Request, credential_id: str):
        with service.db.session() as session:
            device = device_row(session, request)
            credential = session.get(Credential, credential_id, with_for_update=True)
            if not device.user_id or not credential or credential.user_id != device.user_id or credential.kind != 'api':
                raise HTTPException(404, '连接不存在')
            credential.revoked = True
        return {'revoked': True}
