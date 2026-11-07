import io
import json
from datetime import timedelta
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from akb import native
from akb.models import ConnectionTicket, Credential, Job, NativeConnection, Onboarding, User, now
from conftest import register
from sqlalchemy import func, select


def bind(application, client, provider_id):
    kb = client.get('/v1/context').json()['knowledge_base_id']
    with application.state.db.session(kb) as session:
        session.add(NativeConnection(tenant_id=kb, provider_user_id=provider_id, enabled=True))
    return kb


def identity(monkeypatch, provider_id=None, email='drive@example.test'):
    provider_id = provider_id or uuid4().hex
    monkeypatch.setattr(native, 'verify_identity', lambda *_: {'id': provider_id, 'email': email, 'nickname': '文件账号昵称'})
    return provider_id, {'X-Cloudreve-Token': 'valid-fixture-native-token', 'X-AKB-File-User': provider_id}


def fake_drive_login(monkeypatch, provider_id, *, wrong_id=False, missing_token=False):
    class LoginClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, path, json):
            assert path == '/api/v4/session/token'
            assert json['password'] == 'native-password'
            return httpx.Response(200, request=httpx.Request('POST', 'http://fixture' + path), json={
                'code': 0, 'data': {'user': {'id': 'not-the-native-user' if wrong_id else provider_id}, 'token': {
                    'access_token': 'server-separate-access', 'refresh_token': '' if missing_token else 'server-separate-refresh',
                    'access_expires': (now() + timedelta(hours=1)).isoformat(),
                }}})
    monkeypatch.setattr('akb.native_start.httpx.Client', LoginClient)


def stop_sync(application, kb):
    with application.state.db.session(kb) as session:
        connection = session.scalar(select(NativeConnection))
        if connection:
            connection.enabled = False
        for job in session.scalars(select(Job).where(Job.kind == 'native_sync')):
            job.status = 'cancelled'


def test_native_start_reads_drive_binding_not_other_browser_cookie(app, monkeypatch):
    application, client = app
    register(client, '第一个资料库')
    provider_id, headers = identity(monkeypatch)
    kb = bind(application, client, provider_id)
    with application.state.db.session(kb) as session:
        session.scalar(select(Onboarding)).answers = {'preferred_name': '资料库本人'}
    register(client, '另一个已登录资料库')
    with application.state.db.session() as session:
        before = session.scalar(select(func.count()).select_from(Credential))
    try:
        response = client.get('/v1/native/start', headers=headers)
        assert response.status_code == 200
        assert response.json()['onboarding']['knowledge_base_id'] == kb
        assert response.json()['onboarding']['answers']['preferred_name'] == '资料库本人'
        assert 'set-cookie' not in response.headers
        assert client.get('/v1/native/start', headers={**headers, 'X-AKB-File-User': uuid4().hex}).status_code == 409
        assert client.get('/v1/native/start').status_code == 422
        with application.state.db.session() as session:
            assert session.scalar(select(func.count()).select_from(Credential)) == before
    finally:
        stop_sync(application, kb)


def test_native_package_and_launch_are_pinned_and_do_not_fake_connected(app, monkeypatch):
    application, client = app
    register(client)
    provider_id, headers = identity(monkeypatch)
    kb = bind(application, client, provider_id)
    register(client, '错误的环境 Cookie')
    try:
        response = client.post('/v1/native/start/package', headers={**headers, 'Origin': application.state.settings.native_origin})
        assert response.status_code == 200, response.text
        assert 'no-store' in response.headers['cache-control'] and 'set-cookie' not in response.headers
        with ZipFile(io.BytesIO(response.content)) as archive:
            path = next(p for p in archive.namelist() if p.endswith('/.mcp.json'))
            configuration = json.loads(archive.read(path))['mcpServers']
            server = next(iter(configuration.values()))
            assert server['env']['AKB_EXPECTED_KB_ID'] == kb and 'AKB_TOKEN' not in server['env']
        with application.state.db.session(kb) as session:
            ticket = session.scalar(select(ConnectionTicket))
            issuer = session.get(Credential, ticket.issued_by)
            assert issuer.expires_at > ticket.expires_at
            assert issuer.label.startswith('native-start:')
        result = client.post('/v1/native/start/launch', headers=headers)
        assert result.status_code == 201 and result.json()['knowledge_base_id'] == kb
        assert result.json()['launch_url'].startswith('workbuddy://task?') and 'join_' not in result.json()['launch_url']
        assert not client.get('/v1/native/start', headers=headers).json()['onboarding']['connected']
        assert client.post('/v1/native/start/package', headers={**headers, 'Origin': 'https://unrelated.test'}).status_code == 403
        assert client.post('/v1/tokens', headers={**headers, 'Origin': application.state.settings.native_origin}, json={'label': 'no', 'scopes': ['knowledge:read']}).status_code == 403
    finally:
        stop_sync(application, kb)


def test_create_does_not_merge_matching_email_and_repeat_keeps_existing(app, monkeypatch):
    application, client = app
    register(client)
    old_kb = client.get('/v1/context').json()['knowledge_base_id']
    with application.state.db.session() as session:
        old_user_email = session.scalar(select(User).where(User.tenant_id == old_kb)).email
    provider_id, headers = identity(monkeypatch, email=old_user_email)
    assert client.get('/v1/native/start', headers=headers).json() == {
        'linked': False, 'file_account_id': provider_id, 'assistant_mode': 'local'}
    fake_drive_login(monkeypatch, provider_id)
    result = client.post('/v1/native/start/setup', headers=headers, json={'mode': 'create', 'drive_password': 'native-password'})
    assert result.status_code == 200, result.text
    kb = result.json()['onboarding']['knowledge_base_id']
    try:
        assert kb != old_kb and result.json()['onboarding']['answers'] == {} and not result.json()['onboarding']['connected']
        with application.state.db.session(kb) as session:
            connection = session.scalar(select(NativeConnection))
            token = native.token_path(application.state.settings, connection.id)
            assert token.stat().st_mode & 0o777 == 0o600
        repeated = client.post('/v1/native/start/setup', headers=headers, json={'drive_password': 'native-password'})
        assert repeated.json()['onboarding']['knowledge_base_id'] == kb
    finally:
        stop_sync(application, kb)


def test_link_requires_both_accounts_and_keeps_prior_binding(app, monkeypatch):
    application, client = app
    register(client)
    with application.state.db.session() as session:
        user = session.get(User, client.get('/v1/auth/me').json()['id'])
        kb, email = user.tenant_id, user.email
    provider_id, headers = identity(monkeypatch)
    fake_drive_login(monkeypatch, provider_id)
    body = {'mode': 'link', 'drive_password': 'native-password', 'email': email, 'knowledge_password': 'wrong-password'}
    assert client.post('/v1/native/start/setup', headers=headers, json=body).status_code == 401
    body['knowledge_password'] = 'test-password-2026'
    result = client.post('/v1/native/start/setup', headers=headers, json=body)
    assert result.status_code == 200 and result.json()['onboarding']['knowledge_base_id'] == kb
    try:
        register(client, '另一资料库')
        with application.state.db.session() as session:
            body['email'] = session.get(User, client.get('/v1/auth/me').json()['id']).email
        assert client.post('/v1/native/start/setup', headers=headers, json=body).status_code == 409
        assert client.get('/v1/native/start', headers=headers).json()['onboarding']['knowledge_base_id'] == kb
    finally:
        stop_sync(application, kb)


@pytest.mark.parametrize('wrong_id,missing_token,status', [(True, False, 401), (False, True, 503)])
def test_invalid_drive_response_cannot_create_a_library(app, monkeypatch, wrong_id, missing_token, status):
    application, client = app
    provider_id, headers = identity(monkeypatch)
    fake_drive_login(monkeypatch, provider_id, wrong_id=wrong_id, missing_token=missing_token)
    with application.state.db.session() as session:
        count = session.scalar(select(func.count()).select_from(User))
    result = client.post('/v1/native/start/setup', headers=headers, json={'drive_password': 'native-password'})
    assert result.status_code == status
    assert not client.get('/v1/native/start', headers=headers).json()['linked']
    with application.state.db.session() as session:
        assert session.scalar(select(func.count()).select_from(User)) == count


def test_failed_token_persistence_rolls_back_provisioning(app, monkeypatch):
    application, client = app
    provider_id, headers = identity(monkeypatch)
    fake_drive_login(monkeypatch, provider_id)
    def interrupted(settings, connection_id, token):
        path = native.token_path(settings, connection_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('incomplete')
        raise OSError('synthetic persistence failure')
    monkeypatch.setattr(native, 'save_token', interrupted)
    with pytest.raises(OSError):
        client.post('/v1/native/start/setup', headers=headers, json={'drive_password': 'native-password'})
    assert not client.get('/v1/native/start', headers=headers).json()['linked']
    assert not list(application.state.settings.native_secrets_dir.glob('*.json'))
