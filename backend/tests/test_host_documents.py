"""Run the shipped Skill helper using the host runtime, not an installed AKB client."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from akb.config import ROOT
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from reportlab.pdfgen.canvas import Canvas

SCRIPT = ROOT / 'integrations/workbuddy/weknora-skills/personal-knowledge/scripts/host_documents.py'
HOST = Path.home() / '.workbuddy/binaries/python/envs/default/bin/python'
PYTHON = str(HOST if HOST.is_file() else Path(sys.executable))


def run(state, library, *args, ok=True):
    r = subprocess.run([PYTHON, str(SCRIPT), '--library', library, '--origin', 'http://127.0.0.1:5889',
                        '--state', str(state), *map(str, args)], capture_output=True, text=True, timeout=40)
    assert (r.returncode == 0) is ok, r.stderr
    return json.loads(r.stdout if ok else r.stderr)


def configure(state, library, root, **extra):
    config = state.parent / 'config.json'
    config.write_text(json.dumps({'user_authorized': True, 'enabled': True, 'roots': [str(root)], **extra}))
    return run(state, library, 'configure', '--settings', config)


def test_host_runtime_office_pdf_union_incremental_and_scope(tmp_path):
    local = tmp_path / 'teacher'; local.mkdir()
    state = tmp_path / 'cache'; library = str(uuid4())
    d = Document(); d.add_paragraph('教师许宁负责青禾课程，2026年任讲师。'); d.save(local/'履历.docx')
    w = Workbook(); w.active.append(['项目','到账元']); w.active.append(['青禾',186000]); w.save(local/'经费.xlsx')
    p = Presentation(); slide = p.slides.add_slide(p.slide_layouts[1]); slide.shapes.title.text='青禾项目中期检查'; p.save(local/'汇报.pptx')
    c = Canvas(str(local/'certificate.pdf')); c.drawString(20,700,'Teacher Xu Ning certificate QH-2026'); c.save()
    (local/'扫描证书.png').write_bytes(b'not a real image: defer to server')
    (local/'坏文件.docx').write_bytes(b'broken zip')
    (local/'个人说明.txt').write_text('青禾课时为32。', encoding='utf-8')
    secret = tmp_path/'outside.txt'; secret.write_text('禁止扫描的外部资料')
    (local/'越界.txt').symlink_to(secret)
    (local/'.hidden.txt').write_text('不应出现')
    (local/'node_modules').mkdir(); (local/'node_modules'/'dep.txt').write_text('不应出现')
    configure(state,library,local,share_catalog=True)
    scan=run(state,library,'scan')
    assert scan['complete'] and scan['visited']==8 and scan['skipped_unreadable_or_links']==1
    assert scan['status_counts']=={'indexed':5,'deferred':2}
    hits=run(state,library,'search','青禾')['items']
    assert len(hits)==4
    titles = run(state, library, 'search', '青禾', '--titles-only')['items']
    assert {item['id'] for item in titles} == {item['id'] for item in hits}
    assert all(item['snippet'] == '' for item in titles)
    assert len(run(state,library,'search','QH-2026')['items'])==1
    assert not run(state,library,'search','禁止')['items']
    txt=next(h for h in hits if h['path']=='个人说明.txt')
    assert run(state,library,'read','--id',txt['id'],'--hash',txt['sha256'])['blocks'][0]['text']=='青禾课时为32。'
    assert run(state,library,'scan')['changed']==0
    (local/'个人说明.txt').write_text('青禾课时为48。',encoding='utf-8')
    assert not run(state,library,'read','--id',txt['id'],'--hash',txt['sha256'],ok=False)['completed']
    scan=run(state,library,'scan'); assert scan['changed']==1
    assert run(state,library,'search','青禾 48')['items']
    (local/'个人说明.txt').unlink()
    assert run(state,library,'scan')['status_counts']['missing']==1
    sync=run(state,library,'export-sync'); raw=Path(sync['path']).read_bytes(); data=json.loads(raw)
    assert sync['request']['sha256']==hashlib.sha256(raw).hexdigest()
    assert str(local).encode() not in raw and b'186000' not in raw
    assert len(data['catalog']['items'])==7
    configure(state,library,local,enabled=False)
    assert not run(state,library,'search','青禾')['items']
    # Revoked ranges cannot be read even using a saved doc/hash.
    h=hits[0]; assert not run(state,library,'read','--id',h['id'],'--hash',h['sha256'],ok=False)['completed']
    if os.name!='nt': assert state.stat().st_mode & 0o777==0o700


def test_resume_scan_preserves_coverage_and_summary_permission(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("host_docs_resume", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "materials"
    root.mkdir()
    (root / "a").mkdir()
    for path in [root / "1.txt", root / "2.txt", root / "a/3.txt", root / "a/4.txt"]:
        path.write_text("Synthetic lesson planning reference.", encoding="utf-8")
    state, library = tmp_path / "cache", str(uuid4())
    config = {
        "user_authorized": True, "enabled": True, "roots": [str(root)],
        "publish_summary": True,
    }
    index = module.Index(library, "http://127.0.0.1:5889", state)
    index.configure(config)
    with monkeypatch.context() as patch:
        clock = iter(range(20))
        patch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
        first = index.scan(2)
    assert first["visited"] == 2 and first["limit_reason"] == "time_budget"
    assert index.status()["resume_pending"]
    index.db.close()

    index = module.Index(library, "http://127.0.0.1:5889", state)
    with monkeypatch.context() as patch:
        clock = iter(range(20))
        patch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
        last = index.scan(2)
    assert last["complete"] and last["visited"] == 4 and last["changed"] == 4
    assert last["status_counts"] == {"indexed": 4}
    assert index.status()["config"]["publish_summary"] is True
    assert not index.status()["resume_pending"]
    root.rename(tmp_path / "disconnected")
    offline = index.scan()
    assert not offline["complete"] and offline["offline_roots"] == 1
    assert offline["limit_reason"] == "directory_unavailable"
    new_root = tmp_path / "different"
    new_root.mkdir()
    index.configure({"user_authorized": True, "enabled": True, "roots": [str(new_root)]})
    assert index.status()["config"]["publish_summary"] is False
    assert index.status()["last_scan"] is None
    index.db.close()


def test_host_transfer_rejects_foreign_binding_and_unapproved_redirect_before_network(tmp_path):
    spec=importlib.util.spec_from_file_location('host_docs',SCRIPT); mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    library=str(uuid4());idx=mod.Index(library,'http://127.0.0.1:5889',tmp_path/'state')
    ticket=tmp_path/'ticket.json'
    ticket.write_text(json.dumps({'contract':'zhixu-host-transfer-v1','knowledge_base_id':library,'device_id':idx.get('device'),
                                 'url':f'https://untrusted.example/v2/host-transfers/{library}/{uuid4()}','token':'zxt_'+'a'*43}))
    with pytest.raises(ValueError,match='不匹配'):idx.transfer(ticket)
    assert ticket.exists()
    with pytest.raises(ValueError,match='其他空间'):mod.Index(str(uuid4()),'http://127.0.0.1:5889',tmp_path/'state')


def test_all_documents_uses_only_personal_home_and_skip_leaves_no_index(tmp_path, monkeypatch):
    spec=importlib.util.spec_from_file_location('host_docs_all',SCRIPT);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    home=tmp_path/'personal';home.mkdir();(home/'Documents').mkdir()
    (home/'Documents'/'教学资料.txt').write_text('课程青禾，学时32。')
    (home/'Documents'/'账号密码.txt').write_text('PRIVATE_PASSWORD_DO_NOT_INDEX')
    (home/'Documents'/'api_key.txt').write_text('PRIVATE_KEY_DO_NOT_INDEX')
    (home/'secrets').mkdir();(home/'secrets'/'ordinary.txt').write_text('PRIVATE_DIR_DO_NOT_INDEX')
    outside=tmp_path/'other-user';outside.mkdir();(outside/'资料.txt').write_text('OTHER_USER_DO_NOT_INDEX')
    (home/'Documents'/'链接.txt').symlink_to(outside/'资料.txt')
    monkeypatch.setattr(mod.Path,'home',lambda:home)
    idx=mod.Index(str(uuid4()),'http://127.0.0.1:5889',tmp_path/'state')
    assert not idx.config()['enabled']
    with pytest.raises(ValueError,match='授权'):idx.configure({'enabled':True,'all_documents':True})
    conf=idx.configure({'user_authorized':True,'enabled':True,'all_documents':True})
    assert conf['roots']==[str(home)] and not conf['auto_import'] and not conf['share_catalog']
    result=idx.scan(25)
    assert result['complete'] and result['status_counts']=={'indexed':1}
    rows=idx.db.execute('SELECT path,blocks FROM docs').fetchall()
    assert len(rows)==1 and 'PRIVATE' not in str([tuple(x) for x in rows])
    idx.configure({'user_authorized':True,'enabled':False})
    assert idx.db.execute('SELECT count(*) FROM fts').fetchone()[0]==0
