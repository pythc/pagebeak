from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from akb import mcp_server
from akb.evidence_text import date_supported, source_span
from akb.knowledge import choose
from akb.knowledge_schema import KnowledgeAcceptance
from akb.models import Source
from conftest import register
from fastapi.testclient import TestClient
from test_knowledge import draft, fact, source_task, submit


def agent_client(application, browser, scopes=None):
    token = browser.post('/v1/tokens', json={
        'label': '合成采纳测试', 'scopes': scopes or ['knowledge:read', 'knowledge:write', 'profile:read', 'forms:write'],
    }).json()['token']
    client = TestClient(application)
    client.headers['Authorization'] = 'Bearer ' + token
    return client


def adoption(ids, revision=1):
    return {'items': [{'fact_id': fid, 'expected_revision': revision} for fid in ids],
            'reason': '已逐项核对当前原文、人物归属和时间，采纳有直接依据的内容'}


def test_pdf_display_equivalence_keeps_full_value_and_exact_raw_evidence(app, worker):
    _, client = app
    register(client)
    raw = '李若晴获得阿里云\u200bApsara\u00a0Clouder 专项技能认证，采用 EﬃcientNet‑B0，方向为 Cafe\u0301 图像识别。'
    task, es = source_task(client, worker, raw)
    quote = '李若晴获得阿里云Apsara Clouder 专项技能认证，采用 EfficientNet-B0，方向为 Café 图像识别。'
    result, _ = submit(client, task, es, [
        fact(es[0], '阿里云Apsara Clouder 专项技能认证', kind='award', field='title', quote=quote),
        fact(es[0], 'EfficientNet-B0', kind='project', field='title', entity='model', quote=quote),
    ])
    assert result.status_code == 200, result.text
    items = client.get('/v1/knowledge').json()['items']
    assert {item['value'] for item in items} == {'阿里云Apsara Clouder 专项技能认证', 'EfficientNet-B0'}
    assert all(item['quote'] == raw and item['support']['text_matching']['quote'] == 'normalized' for item in items)
    assert client.get(f"/v1/evidence/{es[0]['id']}").json()['text'] == raw


def test_real_value_and_quote_mismatch_still_fail_with_actionable_errors(app, worker):
    _, client = app
    register(client)
    task, es = source_task(client, worker, '李若晴基于Jittor框架在超声图像上做研究，获得Apsara Clouder认证。')
    result, _ = submit(client, task, es, [fact(es[0], '基于Jittor框架', kind='project', field='title', quote='在超声图像上做研究')])
    assert result.status_code == 422 and result.json()['detail']['code'] == 'value_not_in_quote'
    result, _ = submit(client, task, es, [fact(es[0], 'Apsaro Clouder', kind='award', field='title', quote='获得Apsaro Clouder认证')])
    assert result.status_code == 422 and result.json()['detail']['code'] == 'quote_mismatch'
    assert source_span('EfficientNet', 'efficientNet') == (None, None)
    assert source_span('剂量 10²', '剂量 102') == (None, None)
    task, es = source_task(client, worker, '李若晴无\u200b行政职务。')
    absent, _ = submit(client, task, es, [fact(es[0], '无', field='administrative_position', polarity='absent')])
    assert absent.status_code == 200, absent.text


@pytest.mark.parametrize('value,text,expected', [
    ('2023-09', '2023.09–至今', True), ('2023-09-01', '2023.09–至今', False),
    ('2025-03-01', '2025年3月1日', True), ('2025', '2025年毕业', True),
    ('2025-06', '2023/09—2025/06', True), ('2025-02-29', '2025.02.29', False),
])
def test_date_precision_never_invents_missing_month_or_day(value, text, expected):
    assert date_supported(value, text) is expected


def test_month_range_persists_and_is_used_at_original_precision(app, worker):
    application, client = app
    register(client)
    text = '李若晴于2023.09–2025.06担任讲师。'
    task, es = source_task(client, worker, text)
    bad, _ = submit(client, task, es, [fact(es[0], '讲师', valid_from='2023-09-01', time_quote=text)])
    assert bad.status_code == 422 and bad.json()['detail']['code'] == 'date_precision_mismatch'
    good, _ = submit(client, task, es, [fact(es[0], '讲师', valid_from='2023-09', valid_to='2025-06', time_quote=text)])
    assert good.status_code == 200, good.text
    with agent_client(application, client) as agent:
        accepted = agent.post('/v1/knowledge/accept', json=adoption(good.json()['fact_ids']))
        assert accepted.status_code == 200, accepted.text
    stored = client.get('/v1/knowledge').json()['items'][0]
    assert stored['valid_from'] == '2023-09' and stored['valid_to'] == '2025-06'
    query = {'kind': 'employment', 'field': 'job_title', 'as_of': '2025-06-30'}
    assert draft(client, 'job_title', query)['answers'][0]['value'] == '讲师'
    query['as_of'] = '2025-07-01'
    assert draft(client, 'job_title', query)['answers'][0]['status'] == 'missing'


def test_overlapping_date_precision_does_not_claim_a_definitely_newer_value():
    def record(value, start):
        return SimpleNamespace(value=value, valid_from=start, valid_to=None, status='accepted',
                               polarity='positive', basis='agent_proposed')
    assert choose([record('教授', '2025-03'), record('副教授', '2025-03-05')], as_of=date(2026, 1, 1))[0] == 'conflict'
    assert choose([record('讲师', '2023-09'), record('教授', '2025-03')])[1] == '教授'


def test_agent_batch_mcp_attribution_retry_manual_priority_and_revocation(app, worker, monkeypatch):
    application, client = app
    register(client)
    task, es = source_task(client, worker, '李若晴担任副教授，工作单位为澄川学院。')
    result, _ = submit(client, task, es, [fact(es[0], '副教授'), fact(es[0], '澄川学院', field='organization')])
    assert result.status_code == 200
    ids = result.json()['fact_ids']
    assert draft(client, 'job_title')['answers'][0]['status'] == 'missing'
    with agent_client(application, client) as agent:
        def call(method, path, body=None):
            response = agent.request(method, path, json=body)
            assert response.status_code == 200, response.text
            return response.json()
        monkeypatch.setattr(mcp_server, 'call', call)
        first = mcp_server.accept_knowledge_facts(KnowledgeAcceptance(**adoption(ids)))
        assert len(first['accepted']) == 2 and first['user_confirmed'] is False
        directory = client.get('/v1/knowledge').json()
        assert all(f['adopted_by'] == 'agent' for f in directory['items']) and 'Agent 采纳' in directory['markdown']
        assert draft(client, 'job_title')['answers'][0]['value'] == '副教授'
        replay = mcp_server.accept_knowledge_facts(KnowledgeAcceptance(**adoption(ids)))
        assert len(replay['unchanged']) == 2 and not replay['accepted']
        assert client.get('/v1/knowledge').json()['revision'] == directory['revision']
        changed = client.post(f'/v1/knowledge/facts/{ids[0]}/decisions', json={'action': 'correct', 'value': '教授', 'expected_revision': 2})
        assert changed.status_code == 200
        assert agent.post('/v1/knowledge/accept', json=adoption(ids)).status_code == 409
        assert draft(client, 'job_title')['answers'][0]['value'] == '教授'
        # A new file cannot silently override the independent manual correction.
        task, es = source_task(client, worker, '李若晴担任讲师。')
        newer, _ = submit(client, task, es, [fact(es[0], '讲师')])
        blocked = agent.post('/v1/knowledge/accept', json=adoption(newer.json()['fact_ids'])).json()
        assert len(blocked['blocked']) == 1 and not blocked['accepted']
        revoked = client.post(f'/v1/knowledge/facts/{ids[1]}/decisions', json={'action': 'reject', 'expected_revision': 2})
        assert revoked.status_code == 200
        assert agent.post('/v1/knowledge/accept', json=adoption([ids[1]])).status_code == 409
        assert agent.post('/v1/knowledge/accept', json=adoption([ids[1]], revision=3)).json()['blocked']
        assert draft(client, 'organization')['answers'][0]['status'] == 'missing'


def test_adoption_permissions_tenant_version_and_no_scope_reclassification(app, worker):
    application, client = app
    register(client)
    task, es = source_task(client, worker, '李若晴担任教授。', scope='reference')
    result, _ = submit(client, task, es, [fact(es[0], '教授', subject_scope='reference')])
    ids = result.json()['fact_ids']
    with agent_client(application, client, ['knowledge:read', 'profile:read']) as read_only:
        assert read_only.post('/v1/knowledge/accept', json=adoption(ids)).status_code == 403
    with agent_client(application, client) as agent:
        extra = adoption(ids)
        extra['items'][0]['subject_scope'] = 'self'
        assert agent.post('/v1/knowledge/accept', json=extra).status_code == 422
        # A stale item rolls back the entire batch instead of silently applying a prefix.
        batch = adoption(ids + [str(uuid4())])
        assert agent.post('/v1/knowledge/accept', json=batch).status_code == 404
        assert client.get('/v1/knowledge').json()['items'][0]['status'] == 'candidate'
        assert agent.post('/v1/knowledge/accept', json=adoption(ids)).status_code == 200
        assert draft(client, 'job_title')['answers'][0]['status'] == 'missing'
    with TestClient(application) as other:
        register(other)
        with agent_client(application, other) as wrong_tenant:
            assert wrong_tenant.post('/v1/knowledge/accept', json=adoption(ids)).status_code == 404
    task, es = source_task(client, worker, '李若晴的研究方向为计算机视觉。')
    result, _ = submit(client, task, es, [fact(es[0], '计算机视觉', kind='profile', field='research_interests')])
    tenant_id = client.get('/v1/context').json()['knowledge_base_id']
    with application.state.db.session(tenant_id) as session:
        session.get(Source, task['source_id']).sha256 = '0' * 64
    with agent_client(application, client) as agent:
        assert agent.post('/v1/knowledge/accept', json=adoption(result.json()['fact_ids'])).status_code == 409


def test_agent_accepted_conflicts_still_block_forms_and_protect_manual_rejection(app, worker):
    application, client = app
    register(client)
    ids = []
    for value in ['教授', '副教授']:
        task, es = source_task(client, worker, f'李若晴担任{value}。')
        result, _ = submit(client, task, es, [fact(es[0], value)])
        ids.extend(result.json()['fact_ids'])
    with agent_client(application, client) as agent:
        response = agent.post('/v1/knowledge/accept', json=adoption(ids))
        assert response.status_code == 200 and len(response.json()['accepted']) == 2
        assert draft(client, 'job_title')['answers'][0]['status'] == 'conflict'
        client.post(f'/v1/knowledge/facts/{ids[1]}/decisions', json={'action': 'reject', 'expected_revision': 2})
        task, es = source_task(client, worker, '李若晴岗位复核：副教授。')
        again, _ = submit(client, task, es, [fact(es[0], '副教授')])
        assert agent.post('/v1/knowledge/accept', json=adoption(again.json()['fact_ids'])).json()['blocked']
        assert draft(client, 'job_title')['answers'][0]['value'] == '教授'
