import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from akb import guest_space
from akb.api import create_app
from akb.models import NativeConnection, Onboarding, now
from akb.onboarding import WEKNORA_SKILL_VERSION
from akb.weknora_client import private_write
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_assistant_auth import exchange, pending, registered


@pytest.fixture
def guest(test_settings, tmp_path, monkeypatch):
    settings = test_settings.model_copy(update={'knowledge_engine':'weknora','passwordless_enabled':True,
        'assistant_public_origin':'http://127.0.0.1:5876','native_origin':'http://127.0.0.1:5876',
        'assistant_author_email':'fixture@example.test','managed_spaces_dir':tmp_path/'spaces',
        'native_secrets_dir':tmp_path/'native','storage_dir':tmp_path/'objects'})
    calls=[]
    def provision(_settings, tenant_id):
        calls.append(tenant_id)
        identity={'email':'fixture-'+tenant_id+'@zhixu.invalid','provider_id':str(uuid4())}
        private_write(guest_space.managed_path(settings,tenant_id),identity)
        return login(settings,identity)
    def login(_settings,identity):
        return {'user':{'id':identity['provider_id']},'token':{'access_token':str(uuid4()),'refresh_token':str(uuid4()),
            'access_expires':(now()+timedelta(hours=1)).isoformat(),'refresh_expires':(now()+timedelta(days=30)).isoformat()}}
    monkeypatch.setattr(guest_space,'provision_drive',provision)
    monkeypatch.setattr(guest_space,'managed_login',login)
    application=create_app(settings)
    with TestClient(application,base_url=settings.native_origin) as client:
        yield application,client,calls
    for kb in calls:
        with application.state.db.session(kb) as session:
            connection=session.scalar(select(NativeConnection))
            if connection:connection.enabled=False


def welcome(client):
    r=client.get('/v1/welcome');assert r.status_code==200,r.text
    return {'Origin':'http://127.0.0.1:5876','X-CSRF-Token':r.json()['csrf']}


def create(client,headers):
    r=client.post('/v1/welcome/space',json={},headers=headers);assert r.status_code==200,r.text
    return r.json()['knowledge_base_id']


def authorize(client,headers):
    cid,callback=registered(client);ref,verifier=pending(client,cid,callback)
    description=client.get('/v1/welcome/authorization',params={'reference':ref});assert description.status_code==200,description.text
    r=client.post('/v1/welcome/authorization',headers=headers,json={'reference':ref,'nonce':description.json()['nonce'],'allowed':True})
    assert r.status_code==200,r.text
    code=parse_qs(urlsplit(r.json()['redirect_url']).query)['code'][0]
    response=exchange(client,cid,callback,code,verifier);assert response.status_code==200,response.text
    return {'Authorization':'Bearer '+response.json()['access_token']}


def test_no_registration_idempotent_parallel_creation_and_private_sessions(guest):
    app,client,calls=guest
    h=welcome(client)
    assert 'HttpOnly' in client.get('/v1/welcome').headers.get('set-cookie','') or client.cookies.get('zhixu_device')
    assert not calls
    with ThreadPoolExecutor(max_workers=3) as pool:
        ids=list(pool.map(lambda _:create(client,h),range(3)))
    assert len(set(ids))==1 and calls==[ids[0]]
    ready=client.get('/v1/welcome').json()
    assert 'password' not in json.dumps(ready)
    assert ids[0] not in ready['installation']['prompt']
    assert ready['installation']['prompt'].startswith('请使用 expert-manager')
    assert len(ready['installation']['prompt']) < 200
    assert '已有有效连接直接继续' in ready['installation']['prompt']
    a=client.post('/v1/welcome/session',headers=h,json={}).json()
    b=client.post('/v1/welcome/session',headers=h,json={}).json()
    assert a['user']==b['user'] and a['token']!=b['token']
    with app.state.db.session(ids[0]) as s:
        c=s.scalar(select(NativeConnection))
        token=json.loads(guest_space.native.token_path(app.state.settings,c.id).read_text())
        assert token['refresh_token'] not in {a['token']['refresh_token'],b['token']['refresh_token']}


def test_origin_csrf_and_device_identity_cannot_be_supplied_by_body(guest):
    _,client,calls=guest;h=welcome(client)
    assert client.post('/v1/welcome/space',json={},headers={'Origin':h['Origin']}).status_code==403
    assert client.post('/v1/welcome/space',json={},headers={**h,'Origin':'https://evil.test'}).status_code==403
    assert client.post('/v1/welcome/space',json={},headers={'X-CSRF-Token':h['X-CSRF-Token']}).status_code==403
    assert not calls
    kb=create(client,h)
    client.cookies.clear();other=welcome(client)
    second=create(client,other);assert second!=kb
    assert client.get('/v1/onboarding').status_code==401


@pytest.mark.parametrize('choice',['all','folders','none'])
def test_oauth_first_question_only_scope_choice_no_identity_required(guest,choice):
    app,client,_=guest;h=welcome(client);kb=create(client,h);auth=authorize(client,h)
    r=client.post('/v1/onboarding/start',headers=auth,json={'skill_version':WEKNORA_SKILL_VERSION})
    assert r.status_code==200,r.text
    s=r.json();assert s['knowledge_base_id']==kb and [q['key'] for q in s['pending_questions']]==['local_documents']
    assert client.patch('/v1/onboarding',headers=auth,json={'expected_revision':s['revision'],'complete':True}).status_code==422
    done=client.patch('/v1/onboarding',headers=auth,json={'expected_revision':s['revision'],'answers':{'local_documents':choice},'complete':True})
    assert done.status_code==200,done.text
    assert done.json()['answers']=={'local_documents':choice} and not done.json()['pending_questions']
    resumed=client.get('/v1/onboarding',headers=auth).json();assert resumed['answers']==done.json()['answers']
    with app.state.db.session(kb) as session:
        assert session.scalar(select(Onboarding)).skipped==[]


def test_browser_restore_is_single_use_scoped_and_revocation_aware(guest):
    app,client,_=guest;h=welcome(client);kb=create(client,h);auth=authorize(client,h)
    r=client.post('/v1/space/browser-link',headers=auth,json={});assert r.status_code==200,r.text
    code=parse_qs(urlsplit(r.json()['url']).fragment)['space'][0]
    client.cookies.clear();fresh=welcome(client)
    assert client.post('/v1/welcome/restore',headers=fresh,json={'code':code}).status_code==200
    assert create(client,fresh)==kb
    assert client.post('/v1/welcome/restore',headers=fresh,json={'code':code}).status_code==410
    r=client.post('/v1/space/browser-link',headers=auth,json={});code=parse_qs(urlsplit(r.json()['url']).fragment)['space'][0]
    client.cookies.clear();other=welcome(client);create(client,other)
    assert client.post('/v1/welcome/restore',headers=other,json={'code':code}).status_code==409
    from akb.models import Credential
    from akb.security import digest_token
    with app.state.db.session() as s:
        s.scalar(select(Credential).where(Credential.digest==digest_token(auth['Authorization'][7:]))).revoked=True
    client.cookies.clear();third=welcome(client)
    assert client.post('/v1/welcome/restore',headers=third,json={'code':code}).status_code==410


def test_conflicting_native_browser_is_not_overwritten(guest, monkeypatch):
    _, client, _ = guest
    h = welcome(client); create(client, h)
    monkeypatch.setattr(guest_space.native, 'verify_identity', lambda *_: {'id':'other-drive'})
    monkeypatch.setattr(guest_space.native_start, 'linked_tenant', lambda *_: str(uuid4()))
    response = client.post('/v1/welcome/session', headers={**h,'X-Cloudreve-Token':'other'}, json={})
    assert response.status_code == 409
    assert 'token' not in response.json()


def test_managed_worker_session_recovers_only_exact_enabled_binding(guest, monkeypatch):
    app, client, _ = guest
    h=welcome(client);kb=create(client,h)
    with app.state.db.session(kb) as s:
        row=s.scalar(select(NativeConnection));cid=row.id
    path=guest_space.managed_path(app.state.settings,kb)
    identity=json.loads(path.read_text());identity['origin']=app.state.settings.native_cloudreve_url
    private_write(path,identity)
    service=guest_space.native.NativeClient(app.state.settings,cid,kb)
    try:
        assert service.renew_managed_session()['access_token']
        with app.state.db.session(kb) as s:s.get(NativeConnection,cid).enabled=False
        with pytest.raises(OSError):service.renew_managed_session()
        with app.state.db.session(kb) as s:s.get(NativeConnection,cid).enabled=True
        private_write(path,{**identity,'provider_id':'different'})
        with pytest.raises(OSError):service.renew_managed_session()
    finally:service.close()
