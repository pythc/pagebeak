"""Actual stdio extraction/adoption into a synthetic account; never handles real user facts."""

import argparse
import asyncio
import json
import secrets
from uuid import uuid4

import httpx
from akb.config import ROOT, Settings
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(keep_ui=False):
    directory = ROOT / 'work/knowledge-adoption-acceptance' / uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    email, password = f'adoption-{uuid4().hex}@example.test', secrets.token_urlsafe(24)
    report = {'test_kind': 'Real stdio MCP, isolated synthetic account; not WorkBuddy model acceptance',
              'workbuddy_model': 'NOT_RUN'}
    source_ids = []
    api_url = f'http://127.0.0.1:{Settings().api_port}'
    raw = ('李若晴于2023.09–至今在澄川学院担任副教授。\n'
           '李若晴获得阿里云\u200bApsara\u00a0Clouder 专项技能认证，项目采用 EﬃcientNet‑B0。\n'
           '李若晴基于Jittor框架在超声图像上完成研究。')
    with httpx.Client(base_url=api_url, trust_env=False, timeout=40) as browser:
        response = browser.post('/v1/auth/register', json={'email': email, 'password': password, 'display_name': 'Agent采纳合成验收'})
        response.raise_for_status()
        browser.headers['X-CSRF-Token'] = response.json()['csrf']
        issued = None
        try:
            kb = browser.get('/v1/context').json()['knowledge_base_id']
            report['knowledge_base_id'] = kb
            issued = browser.post('/v1/tokens', json={'label': '合成采纳协议验收',
                'scopes': ['knowledge:read', 'knowledge:write', 'profile:read', 'forms:write']})
            issued.raise_for_status()
            server = StdioServerParameters(command=str(ROOT / '.venv/bin/python'), args=['-m', 'akb.mcp_server'],
                cwd=str(ROOT), env={'AKB_API_URL': api_url, 'AKB_TOKEN': issued.json()['token'], 'AKB_EXPECTED_KB_ID': kb})
            with (directory / 'mcp-stderr.log').open('w') as errors:
                async with stdio_client(server, errlog=errors) as (read, write), ClientSession(read, write) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    assert len(names) == 37 and 'accept_knowledge_facts' in names
                    report['mcp_tools'] = len(names)

                    async def invoke(name, arguments=None, rejected=None):
                        result = await session.call_tool(name, arguments or {})
                        content = result.content[0].text
                        if rejected:
                            assert result.isError and rejected in content, content
                            return
                        assert not result.isError, content
                        return json.loads(content)

                    await invoke('get_knowledge_context', {'expected_knowledge_base_id': kb})
                    imported = await invoke('import_text', {'title': '合成履历与证书', 'text': raw,
                        'request_id': uuid4().hex, 'subject_scope': 'self'})
                    source_id = imported['source']['id']
                    source_ids.append(source_id)
                    for _ in range(80):
                        job = await invoke('get_job', {'job_id': imported['job_id']})
                        if job['status'] == 'completed':
                            break
                        assert job['status'] not in {'failed', 'cancelled'}, job
                        await asyncio.sleep(0.25)
                    assert job['status'] == 'completed'
                    task = next(t for t in (await invoke('list_knowledge_tasks'))['items'] if t['source_id'] == source_id)
                    lease = await invoke('claim_knowledge_task', {'task_id': task['id']})
                    evidence = (await invoke('read_source', {'source_id': source_id, 'limit': 30}))['items']

                    def item(index, kind, field, value, **extra):
                        return {'kind': kind, 'field': field, 'value': value, 'subject': '李若晴', 'subject_scope': 'self',
                                'entity_key': f'{kind}/current', 'evidence_id': evidence[index]['id'],
                                'quote': evidence[index]['text'], **extra}

                    async def submit(items, rejected=None):
                        return await invoke('submit_knowledge_task', {'task_id': task['id'], 'submission': {
                            'items': items, 'expected_revision': lease['revision'], 'request_id': uuid4().hex,
                            'complete': True, 'reviewed_evidence_ids': [e['id'] for e in evidence]}}, rejected)

                    await submit([item(0, 'employment', 'job_title', '副教授', valid_from='2023-09-01',
                                       time_quote=evidence[0]['text'])], 'date_precision_mismatch')
                    await submit([item(2, 'project', 'title', '基于Jittor框架', quote='在超声图像上完成研究')], 'value_not_in_quote')
                    await submit([item(1, 'award', 'title', 'Apsaro Clouder', quote='获得Apsaro Clouder认证')], 'quote_mismatch')
                    submitted = await submit([
                        item(0, 'employment', 'job_title', '副教授', valid_from='2023-09', time_quote=evidence[0]['text']),
                        item(1, 'award', 'title', '阿里云Apsara Clouder 专项技能认证',
                             quote='李若晴获得阿里云Apsara Clouder 专项技能认证，项目采用 EfficientNet-B0。'),
                        item(2, 'project', 'title', '基于Jittor框架在超声图像上完成研究'),
                    ])
                    before = await invoke('read_knowledge')
                    acceptance = {'items': [{'fact_id': fid, 'expected_revision': 1} for fid in submitted['fact_ids']],
                                  'reason': '已读取本文件全部当前证据，核对主体、完整名称及月级时间，按原文采纳'}
                    accepted = await invoke('accept_knowledge_facts', {'acceptance': acceptance})
                    assert len(accepted['accepted']) == 3 and accepted['user_confirmed'] is False
                    directory_view = await invoke('read_knowledge')
                    assert directory_view['revision'] > before['revision'] and directory_view['pending']['candidates'] == 0
                    assert all(f['adopted_by'] == 'agent' for f in directory_view['items'])
                    employment = next(f for f in directory_view['items'] if f['kind'] == 'employment')
                    award = next(f for f in directory_view['items'] if f['kind'] == 'award')
                    assert employment['valid_from'] == '2023-09'
                    assert award['value'] == '阿里云Apsara Clouder 专项技能认证' and award['quote'] == evidence[1]['text']
                    replay = await invoke('accept_knowledge_facts', {'acceptance': acceptance})
                    assert len(replay['unchanged']) == 3 and not replay['accepted']
                    assert (await invoke('read_knowledge'))['revision'] == directory_view['revision']
                    catalog = await invoke('read_catalog')
                    assert 'Agent 采纳' in str(catalog)
                    prepared = await invoke('prepare_form', {'title': '合成教师登记', 'fields': [
                        {'key': 'job_title', 'label': '职称', 'fact_key': 'job_title'}]})
                    assert prepared['answers'][0]['value'] == '副教授'
                    changed = browser.post(f"/v1/knowledge/facts/{employment['id']}/decisions", json={
                        'action': 'correct', 'expected_revision': 2, 'value': '教授'})
                    changed.raise_for_status()
                    stale = await invoke('read_form_draft', {'draft_id': prepared['id']})
                    assert stale['stale']
                    await invoke('accept_knowledge_facts', {'acceptance': acceptance}, rejected='409')
                    assert (await invoke('get_profile'))['fields']['job_title']['value'] == '教授'
                    report.update({'error_cases_rejected': ['invented_day', 'value_not_in_quote', 'changed_quote'],
                        'month_precision_preserved': True, 'full_certificate_name_preserved': True,
                        'raw_evidence_unchanged': True, 'adopted_by_agent': 3, 'retry_does_not_mutate': True,
                        'shared_catalog_attribution': True, 'form_value': '副教授', 'manual_correction_preserved': True,
                        'old_draft_invalidated': True, 'result': 'PASS'})
        finally:
            if issued is not None and issued.is_success:
                response = browser.delete('/v1/tokens/' + issued.json()['id'])
                report['synthetic_api_token_revoked'] = response.status_code == 204
            if keep_ui and report.get('result') == 'PASS':
                path = directory / 'ui-session.json'
                path.write_text(json.dumps({'email': email, 'password': password, 'source_ids': source_ids}))
                path.chmod(0o600)
                report['synthetic_ui_cleanup'] = 'pending browser verification'
            else:
                for source_id in source_ids:
                    browser.delete('/v1/sources/' + source_id).raise_for_status()
                report['synthetic_source_cleanup'] = 'requested'
            browser.post('/v1/auth/logout')
            (directory / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'result': report['result'], 'report': str(directory / 'verification.json')}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--keep-ui', action='store_true', help='Keep only synthetic data for subsequent UI verification')
    asyncio.run(main(parser.parse_args().keep_ui))
