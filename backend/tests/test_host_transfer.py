"""Hash-bound, expiring and revocable host transfers; no general API credential."""
import hashlib
import json
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from akb.models import Credential, HostTransfer, LocalSourceLink, now
from akb.security import digest_token
from conftest import register
from sqlalchemy import select


def setup(app):
    application, client=app
    register(client)
    tenant=client.get('/v1/context').json()['knowledge_base_id']
    token=client.post('/v1/tokens',json={'label':'host-test','scopes':['knowledge:read','knowledge:write','profile:read']}).json()['token']
    return application,client,tenant,{'Authorization':'Bearer '+token}


def upload_body(data):
    return {'request_id':str(uuid4()),'device_id':str(uuid4()),'document_id':str(uuid4()),
            'file':{'filename':'合成履历.txt','size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'subject_scope':'self'}}


def ticket(client, body, headers, kind='upload'):
    r=client.post('/v2/host-transfers/'+kind,json=body,headers=headers)
    assert r.status_code==200,r.text
    t=r.json();return t,urlsplit(t['url']).path,{'Authorization':'Bearer '+t['token']}


def test_upload_resume_exact_bytes_replay_and_parent_revocation(app):
    application,client,tenant,h=setup(app)
    data='合成教师程亦舟，青禾课程32课时。'.encode()
    body=upload_body(data);t,path,grant=ticket(client,body,h)
    assert client.put(path,content=data[:9],headers=grant).status_code==200
    assert client.get(path,headers=grant).json()['upload']['offset']==9
    assert client.put(path+'?offset=0',content=data[9:],headers=grant).status_code==409
    assert client.post(path+'/complete',headers=grant).status_code in {409,422}
    assert client.put(path+'?offset=9',content=data[9:],headers=grant).status_code==200
    result=client.post(path+'/complete',headers=grant)
    assert result.status_code==200,result.text
    source=result.json()['source']
    assert source['sha256']==body['file']['sha256']
    assert client.post(path+'/complete',headers=grant).json()==result.json()
    with application.state.db.session(tenant)as session:
        link=session.scalar(select(LocalSourceLink).where(LocalSourceLink.source_id==source['id']))
        assert link.device_id==body['device_id'] and not link.stale
    t2,path2,g2=ticket(client,body,h)
    assert path2==path and t['token']!=t2['token']
    assert client.get(path,headers=grant).status_code==401
    assert client.get(path,headers=g2).json()['result']==result.json()
    assert client.post('/v2/host-transfers/upload',headers=h,json=body|{'document_id':str(uuid4())}).status_code==409
    # Ticket does not authenticate as a normal API key.
    assert client.get('/v1/context',headers=g2).status_code==401
    with application.state.db.session()as session:
        row=session.scalar(select(Credential).where(Credential.digest==digest_token(h['Authorization'][7:])))
        row.revoked=True
    assert client.get(path,headers=g2).status_code==401


def test_foreign_tenant_expiration_and_hash_bound_catalog(app):
    application,client,tenant,h=setup(app)
    body=upload_body(b'one');_,path,g=ticket(client,body,h)
    assert client.get(path.replace(tenant,str(uuid4())),headers=g).status_code==401
    with application.state.db.session(tenant)as session:
        row=session.get(HostTransfer,path.rsplit('/',1)[-1]);row.expires_at=now()-timedelta(seconds=1)
    assert client.put(path,content=b'one',headers=g).status_code==401
    device=str(uuid4())
    payload={'catalog':{'device_id':device,'snapshot_id':str(uuid4()),'expected_revision':0,'items':[]},
             'observations':{'device_id':device,'items':[]}}
    data=json.dumps(payload).encode()
    body={'request_id':str(uuid4()),'device_id':device,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    _,path,g=ticket(client,body,h,'catalog')
    assert client.put(path,content=data+b' ',headers=g).status_code==413
    assert client.put(path,content=data[:-1],headers=g).status_code==422
    assert client.post(path+'/complete',headers=g).status_code==403
    result=client.put(path,content=data,headers=g)
    assert result.status_code==200,result.text
    assert result.json()['catalog']['revision']==1
    assert client.put(path,content=data,headers=g).json()==result.json()
    # Valid hash for another device is insufficient.
    _,path,g=ticket(client,body|{'request_id':str(uuid4()),'device_id':str(uuid4())},h,'catalog')
    assert client.put(path,content=data,headers=g).status_code==403


def test_wrong_file_hash_never_creates_source(app):
    _,client,_,h=setup(app)
    data=b'actual';body=upload_body(b'forged')
    _,path,g=ticket(client,body,h)
    assert client.put(path,content=data,headers=g).status_code==200
    response=client.post(path+'/complete',headers=g)
    assert response.status_code==409,response.text


def test_host_chunk_over_one_mib_passes_global_body_guard(app):
    _,client,_,h=setup(app)
    data=b'x'*(2*1024**2)
    _,path,g=ticket(client,upload_body(data),h)
    r=client.put(path,content=data,headers=g)
    assert r.status_code==200 and r.json()['offset']==len(data),r.text
    assert client.post(path+'/complete',headers=g).status_code==200
