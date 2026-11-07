import io
import json
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

from akb.assistant_package import NAME, build_assistant, connection_name, installation
from akb.config import ROOT, Settings


def test_start_prompt_is_brief_and_contains_no_expiring_authorization_or_personal_id():
    settings = Settings(assistant_public_origin='https://documents.example.test',
                        assistant_author_email='maintainer@example.test', knowledge_engine='weknora')
    kb = 'f6ac97b5-159a-46a3-a8da-b8e70c623111'
    for target in [None, kb]:
        result = installation(settings, target)
        prompt = result['prompt']
        assert len(prompt) < 200
        assert 'expert-manager' in prompt and '下载并原样安装' in prompt
        assert result['package_url'] in prompt
        assert result['package_url'] in prompt.splitlines()
        assert '已有有效连接直接继续' in prompt and '未连接时发起正常授权' in prompt
        assert 'assistant_request=' not in prompt and kb not in prompt and result['sha256'] not in prompt
        assert parse_qs(urlsplit(result['launch_url']).query)['prompt'] == [prompt]


def test_expert_declares_native_connection_dependency_and_actionable_auth_instructions():
    origin = 'https://documents.example.test'
    expert, _ = build_assistant(origin, 'maintainer@example.test',
        (ROOT / 'integrations/workbuddy/assets/zhixu-512.png').read_bytes(), engine='weknora')
    key = connection_name(origin)
    with ZipFile(io.BytesIO(expert)) as bundle:
        manifest = json.loads(bundle.read(NAME + '/.codebuddy-plugin/plugin.json'))
        assert manifest['dependencies']['mcpServers'] == './.mcp.json'
        remote = json.loads(bundle.read(NAME + '/.mcp.json'))
        assert list(remote['mcpServers']) == [key]
        guide = bundle.read(NAME + '/skills/personal-knowledge/references/connection.md').decode()
        agent = bundle.read(NAME + '/agents/teacher-assistant.md').decode()
        assert 'workbuddy_request_mcp_connection' in guide and 'workbuddy_request_mcp_connection' in agent
        assert key in guide and guide.count(key) == 1
        assert key + key.removeprefix(NAME) not in guide
        assert '不复用历史 assistant_request' in guide and 'connected' in guide
        assert 'get_knowledge_context' in agent and 'expected_service_origin' in agent
        assert 'expected_knowledge_base_id' in guide
        assert guide.index('先检查现有连接') < guide.index('未连接或授权明确失效时')
        assert '不再询问“是否使用现有连接继续”' in guide
        assert '不保证每个版本或会话都提供' in guide
        assert '检索明确不可用时，不再尝试直接调用不存在的工具' in guide
        assert '不要求重新授权' in agent and '不是初始化前置条件' in agent
