"""Synthetic identities only. Real JWT validation and OAuth/MCP routes, mocked upstream HTTP."""
import asyncio
import json
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import jwt
import pytest
from akb import guest_space, mcp_server
from akb.api import create_app
from akb.identity import IdentityService
from akb.models import Catalog, ExternalIdentity, IdentityLogin, Source, User, now
from akb.security import digest_token
from akb.weknora_client import private_write
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_assistant_auth import exchange, pending
from test_guest_space import welcome


@pytest.fixture
def federated(test_settings, tmp_path, monkeypatch):
    config = tmp_path / 'providers.json'
    config.write_text(json.dumps([{'key': 'example', 'label': '测试账号', 'issuer': 'https://id.example.test',
        'client_id': 'pagebeak-fixture', 'client_secret': secrets.token_urlsafe(24),
        'authorization_endpoint': 'https://id.example.test/authorize',
        'token_endpoint': 'https://id.example.test/token', 'jwks_uri': 'https://id.example.test/jwks'},
        {'key': 'workbuddy', 'label': 'WorkBuddy', 'kind': 'workbuddy', 'issuer': 'https://www.workbuddy.cn',
         'client_id': 'workbuddy-fixture', 'client_secret': secrets.token_urlsafe(24),
         'authorization_endpoint': 'https://www.workbuddy.cn/openapi/v2/authorize',
         'token_endpoint': 'https://www.workbuddy.cn/openapi/v2/token',
         'token_auth': 'client_secret_post', 'scopes': ['user.profile.readable']}]))
    settings = test_settings.model_copy(update={'knowledge_engine': 'weknora', 'passwordless_enabled': True,
        'assistant_public_origin': 'http://127.0.0.1:5876', 'native_origin': 'http://127.0.0.1:5876',
        'identity_required': True, 'identity_providers_file': config, 'storage_dir': tmp_path / 'objects',
        'managed_spaces_dir': tmp_path / 'spaces', 'native_secrets_dir': tmp_path / 'native'})
    drives = []

    def provision(_settings, tenant_id):
        drives.append(tenant_id)
        identity = {'provider_id': str(uuid4()), 'email': 'fixture@zhixu.invalid'}
        private_write(guest_space.managed_path(settings, tenant_id), identity)
        return {'user': {'id': identity['provider_id']}, 'token': {'access_token': str(uuid4()),
            'refresh_token': str(uuid4()), 'access_expires': (now() + timedelta(hours=1)).isoformat(),
            'refresh_expires': (now() + timedelta(days=30)).isoformat()}}

    monkeypatch.setattr(guest_space, 'provision_drive', provision)
    app = create_app(settings)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid='fixture-key', use='sig', alg='RS256')
    codes = {}
    captured = []

    def identity_http(request):
        captured.append(request)
        if request.url.path == '/jwks':
            return httpx.Response(200, json={'keys': [jwk]})
        params = parse_qs(request.content.decode())
        info = codes.pop(params['code'][0])
        if request.url.host == 'www.workbuddy.cn':
            return httpx.Response(200, json={'open_id': info['sub'], 'token_type': 'Bearer',
                'access_token': 'upstream-secret-not-persisted', 'refresh_token': 'upstream-refresh-not-persisted'})
        claims = {'iss': 'https://id.example.test', 'aud': 'pagebeak-fixture', 'sub': info['sub'],
                  'iat': int(now().timestamp()), 'exp': int((now() + timedelta(minutes=5)).timestamp()),
                  'nonce': info['nonce'], **info.get('overrides', {})}
        signing_key = info.get('key', key)
        return httpx.Response(200, json={'id_token': jwt.encode(claims, signing_key, algorithm='RS256',
                                                              headers={'kid': 'fixture-key'})})

    monkeypatch.setattr(app.state.identity_service, 'http_client',
                        lambda: httpx.Client(transport=httpx.MockTransport(identity_http)))
    with TestClient(app, base_url=settings.native_origin) as client:
        yield app, client, codes, captured, drives


def begin(client, codes, subject, *, provider='example', intent='signin', overrides=None, reference=None):
    headers = welcome(client)
    result = client.post('/v1/welcome/identity/start', headers=headers,
        json={'provider': provider, 'intent': intent, **({'reference': reference} if reference else {})})
    assert result.status_code == 200, result.text
    params = parse_qs(urlsplit(result.json()['authorization_url']).query)
    code = secrets.token_urlsafe(24)
    codes[code] = {'sub': subject, 'nonce': params.get('nonce', [''])[0], 'overrides': overrides or {}}
    return params['state'][0], code


def finish(client, state, code):
    return client.get('/v1/welcome/identity/callback', params={'state': state, 'code': code}, follow_redirects=False)


def signin(client, codes, subject, **kwargs):
    state, code = begin(client, codes, subject, **kwargs)
    result = finish(client, state, code)
    assert result.status_code == 303, result.text
    return client.get('/v1/welcome').json()


def connect(client, callback, name):
    result = client.post('/register', json={'redirect_uris': [callback], 'client_name': name,
        'token_endpoint_auth_method': 'none', 'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code']})
    assert result.status_code == 201, result.text
    cid = result.json()['client_id']
    reference, verifier = pending(client, cid, callback)
    headers = welcome(client)
    result = client.post('/v1/welcome/space', json={}, headers=headers)
    assert result.status_code == 200, result.text
    description = client.get('/v1/welcome/authorization', params={'reference': reference})
    assert description.status_code == 200, description.text
    approved = client.post('/v1/welcome/authorization', headers=headers,
        json={'reference': reference, 'nonce': description.json()['nonce'], 'allowed': True})
    code = parse_qs(urlsplit(approved.json()['redirect_url']).query)['code'][0]
    tokens = exchange(client, cid, callback, code, verifier)
    assert tokens.status_code == 200, tokens.text
    return cid, tokens.json(), result.json()['knowledge_base_id']


def test_same_account_new_browser_two_different_mcp_clients_share_space_and_revoke_independently(federated, monkeypatch):
    app, client, codes, _, drives = federated
    before = client.get('/v1/welcome').json()
    assert not before['has_space'] and not before['identity']['browser_is_identity']
    assert client.post('/v1/welcome/space', headers=welcome(client), json={}).status_code == 401
    subject = uuid4().hex
    signin(client, codes, subject)
    with app.state.db.session() as session:
        first = session.scalar(select(ExternalIdentity).order_by(ExternalIdentity.created_at.desc()))
        subject, user_id = first.subject, first.user_id
    _, first_tokens, tenant = connect(client, 'workbuddy://workbuddy/oauth/callback', 'WorkBuddy fixture')
    with app.state.db.session(tenant) as session:
        session.scalar(select(Catalog)).manual = {'notes': '用户手工整理：保留原文件，按项目归档。'}
    old_cookie = client.cookies.get('zhixu_device')
    client.cookies.clear()
    fresh = signin(client, codes, subject)
    assert fresh['has_space'] and fresh['identity']['verified']
    assert client.cookies.get('zhixu_device') != old_cookie
    _, second_tokens, second = connect(client, 'com.example.agent:/oauth/callback', 'Independent Agent fixture')
    assert second == tenant and drives == [tenant]
    with app.state.db.session(tenant) as session:
        assert session.scalar(select(Catalog)).manual == {'notes': '用户手工整理：保留原文件，按项目归档。'}
        assert session.get(User, user_id).tenant_id == tenant

    def api(method, path, body=None, **kwargs):
        current = mcp_server.connection()
        response = client.request(method, path, json=body, headers={
            'Authorization': 'Bearer ' + current['token'], 'X-AKB-Knowledge-Base': current['expected']})
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(mcp_server, 'call', api)
    for tokens in (first_tokens, second_tokens):
        response = client.post('/mcp', headers={'Authorization': 'Bearer ' + tokens['access_token'],
            'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'get_knowledge_context', 'arguments': {'expected_knowledge_base_id': tenant}}})
        assert response.status_code == 200 and not response.json()['result'].get('isError'), response.text
    connections = client.get('/v1/welcome/connections').json()
    assert {c['client_name'] for c in connections} == {'WorkBuddy fixture', 'Independent Agent fixture'}
    one = next(c for c in connections if c['client_name'] == 'WorkBuddy fixture')
    assert client.post('/v1/welcome/connections/' + one['id'] + '/revoke', json={}, headers=welcome(client)).status_code == 200
    provider = app.state.assistant_authorization
    assert asyncio.run(provider.load_access_token(first_tokens['access_token'])) is None
    assert asyncio.run(provider.load_access_token(second_tokens['access_token'])) is not None


@pytest.mark.parametrize('override', [
    {'iss': 'https://forged.example'}, {'aud': 'different-client'}, {'nonce': 'forged-nonce'},
    {'exp': 1}, {'sub': ''}, {'aud': ['pagebeak-fixture', 'other'], 'azp': 'other'},
])
def test_reject_wrong_issuer_audience_nonce_expiry_subject_and_authorized_party(federated, override):
    app, client, codes, _, drives = federated
    state, code = begin(client, codes, uuid4().hex, overrides=override)
    assert finish(client, state, code).status_code == 401
    assert finish(client, state, code).status_code == 410
    assert not client.get('/v1/welcome').json()['has_space'] and not drives


def test_wrong_signature_and_cross_browser_state_are_rejected(federated):
    _, client, codes, _, _ = federated
    state, code = begin(client, codes, uuid4().hex)
    cookie = client.cookies.get('zhixu_device')
    client.cookies.clear()
    welcome(client)
    assert finish(client, state, code).status_code == 410
    client.cookies.clear()
    client.cookies.set('zhixu_device', cookie)
    codes[code]['key'] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert finish(client, state, code).status_code == 401


def test_callback_expiry_csrf_origin_and_untrusted_supplied_identity(federated):
    app, client, codes, _, _ = federated
    headers = welcome(client)
    assert client.post('/v1/welcome/identity/start', json={'provider': 'example'}).status_code == 403
    assert client.post('/v1/welcome/identity/start', headers={**headers, 'Origin': 'https://evil.test'},
        json={'provider': 'example'}).status_code == 403
    assert client.post('/v1/welcome/identity/start', headers=headers,
        json={'provider': 'example', 'user_id': 'guessed'}).status_code == 422
    state, code = begin(client, codes, uuid4().hex)
    with app.state.db.session() as session:
        session.scalar(select(IdentityLogin).where(IdentityLogin.digest == digest_token(state))).expires_at = now() - timedelta(seconds=1)
    assert finish(client, state, code).status_code == 410


def test_legacy_space_requires_explicit_binding_preserves_tenant_and_manual_edits(federated):
    app, client, codes, _, drives = federated
    app.state.settings.identity_required = False
    headers = welcome(client)
    result = client.post('/v1/welcome/space', headers=headers, json={})
    tenant = result.json()['knowledge_base_id']
    with app.state.db.session(tenant) as session:
        session.scalar(select(Catalog)).manual = {'notes': '人工备注，不能被账号迁移覆盖'}
    app.state.settings.identity_required = True
    subject = uuid4().hex
    state, code = begin(client, codes, subject)
    assert finish(client, state, code).status_code == 409
    signin(client, codes, subject, intent='link')
    assert client.post('/v1/welcome/space', headers=welcome(client), json={}).json()['knowledge_base_id'] == tenant
    with app.state.db.session(tenant) as session:
        assert session.scalar(select(Catalog)).manual == {'notes': '人工备注，不能被账号迁移覆盖'}
    assert drives == [tenant]


def test_accounts_are_isolated_and_cannot_be_merged_by_client_name_or_link(federated):
    app, client, codes, _, _ = federated
    subject_a, subject_b = uuid4().hex, uuid4().hex
    signin(client, codes, subject_a)
    _, token_a, tenant_a = connect(client, 'https://client.example/a', 'Same client name')
    client.cookies.clear()
    signin(client, codes, subject_b)
    _, token_b, tenant_b = connect(client, 'https://client.example/b', 'Same client name')
    assert tenant_a != tenant_b
    state, code = begin(client, codes, subject_a, intent='link')
    assert finish(client, state, code).status_code == 409
    headers_a, headers_b = ({'Authorization': 'Bearer ' + t['access_token']} for t in (token_a, token_b))
    assert client.get('/v1/capabilities', headers={**headers_b, 'X-AKB-Knowledge-Base': tenant_a}).status_code == 409
    with app.state.db.session(tenant_a) as session:
        source = Source(tenant_id=tenant_a, title='A only', filename='A-only.txt', media_type='text/plain', size=1,
                        sha256='0' * 64, object_key='fixture', subject_scope='self')
        session.add(source)
        session.flush()
        sid = source.id
    assert client.get('/v1/sources/' + sid, headers=headers_a).status_code == 200
    assert client.get('/v1/sources/' + sid, headers=headers_b).status_code == 404


def test_workbuddy_adapter_uses_server_token_response_and_never_keeps_upstream_tokens(federated):
    app, client, codes, captured, _ = federated
    subject = 'op_' + uuid4().hex
    signin(client, codes, subject, provider='workbuddy')
    with app.state.db.session() as session:
        identity = session.scalar(select(ExternalIdentity).where(ExternalIdentity.subject == subject))
        assert identity.namespace == 'workbuddy-fixture'
        transactions = list(session.scalars(select(IdentityLogin).where(IdentityLogin.device_id ==
            session.scalar(select(IdentityLogin.device_id).order_by(IdentityLogin.expires_at.desc())))))
        assert 'upstream-secret' not in str([row.__dict__ for row in transactions])
    request = next(r for r in captured if r.url.host == 'www.workbuddy.cn')
    params = parse_qs(request.content.decode())
    assert params['client_id'] == ['workbuddy-fixture'] and params['client_secret']
    public = json.dumps(client.get('/v1/welcome').json())
    assert 'upstream-secret' not in public and 'client_secret' not in public


def test_logout_rotates_session_and_does_not_revoke_other_client_grants(federated):
    app, client, codes, _, _ = federated
    signin(client, codes, uuid4().hex)
    _, tokens, _ = connect(client, 'http://127.0.0.1:4242/callback', 'Loopback client')
    before = client.cookies.get('zhixu_device')
    assert client.post('/v1/welcome/identity/logout', json={}, headers=welcome(client)).status_code == 200
    assert not client.get('/v1/welcome').json()['has_space']
    assert client.cookies.get('zhixu_device') != before
    assert asyncio.run(app.state.assistant_authorization.load_access_token(tokens['access_token'])) is not None


def test_startup_fails_closed_without_provider_and_legacy_register_is_disabled(federated):
    app, client, _, _, _ = federated
    with pytest.raises(ValueError, match='配置身份提供方'):
        IdentityService(app.state.db, app.state.settings.model_copy(update={'identity_providers_file': None}))
    response = client.post('/v1/auth/register', json={'email': 'synthetic@example.test',
        'password': 'synthetic-password-only', 'display_name': 'Synthetic'})
    assert response.status_code == 404
    assert client.get('/v1/assistant/connection').json()['transport'] == 'streamable_http'
    assert client.post('/register', json={'redirect_uris': ['https://client.example/oauth'],
        'token_endpoint_auth_method': 'none'}).status_code == 201


def test_provisioning_retry_keeps_same_verified_user_and_tenant(federated, monkeypatch):
    app, client, codes, _, drives = federated
    signin(client, codes, uuid4().hex)
    original = guest_space.provision_drive
    attempts = []

    def fail_once(settings, tenant_id):
        attempts.append(tenant_id)
        if len(attempts) == 1:
            from fastapi import HTTPException
            raise HTTPException(503, 'Synthetic timeout')
        return original(settings, tenant_id)

    monkeypatch.setattr(guest_space, 'provision_drive', fail_once)
    headers = welcome(client)
    assert client.post('/v1/welcome/space', headers=headers, json={}).status_code == 503
    retry = client.post('/v1/welcome/space', headers=headers, json={})
    assert retry.status_code == 200 and attempts[0] == attempts[1] == retry.json()['knowledge_base_id']
    assert drives == [attempts[0]]


def test_logout_during_login_and_changed_provider_configuration_reject_callback(federated):
    app, client, codes, _, _ = federated
    state, code = begin(client, codes, uuid4().hex)
    assert client.post('/v1/welcome/identity/logout', headers=welcome(client), json={}).status_code == 200
    welcome(client)
    assert finish(client, state, code).status_code == 410
    state, code = begin(client, codes, uuid4().hex)
    app.state.identity_service.providers['example'].client_id = 'changed-client'
    assert finish(client, state, code).status_code == 409


@pytest.mark.parametrize('redirect', ['javascript:alert(1)', 'data:text/plain,hello', 'file:///tmp/secret',
                                       'https://user:password@client.example/callback', 'http://untrusted.example/callback'])
def test_unsafe_callback_registration_is_rejected(federated, redirect):
    _, client, _, _, _ = federated
    assert client.post('/register', json={'redirect_uris': [redirect],
        'token_endpoint_auth_method': 'none'}).status_code == 400


def test_disabled_drive_is_not_replaced_by_identity_login(federated):
    app, client, codes, _, drives = federated
    signin(client, codes, uuid4().hex)
    _, _, tenant = connect(client, 'https://client.example/oauth', 'Synthetic client')
    from akb.models import NativeConnection
    with app.state.db.session(tenant) as session:
        session.scalar(select(NativeConnection)).enabled = False
    assert client.post('/v1/welcome/space', headers=welcome(client), json={}).status_code == 409
    assert drives == [tenant]
