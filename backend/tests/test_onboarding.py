import errno
import importlib.util
import json
import os
import shlex
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from akb.config import ROOT
from akb.models import ConnectionTicket, Credential, now
from akb.onboarding import SKILL_VERSION
from conftest import register
from fastapi.testclient import TestClient
from sqlalchemy import select


def ticket(client):
    response = client.post("/v1/onboarding/connection")
    assert response.status_code == 201, response.text
    value = response.json()
    command = next(line for line in value["prompt"].splitlines() if "--code" in line)
    args = shlex.split(command)
    return {"code": args[args.index("--code") + 1], "knowledge_base_id": value["knowledge_base_id"]}, value


def redeem(client):
    body, prompt = ticket(client)
    response = client.post("/v1/onboarding/redeem", json=body)
    assert response.status_code == 200, response.text
    return response.json(), prompt


def agent(client, connection):
    token = connection["config"]["env"]["AKB_TOKEN"]
    return {"Authorization": "Bearer " + token,
            "X-AKB-Knowledge-Base": connection["config"]["env"]["AKB_EXPECTED_KB_ID"]}


def test_ticket_one_use_pinned_and_installed_is_not_connected(app):
    _, client = app
    register(client, "登录昵称不应当作姓名")
    body, prompt = ticket(client)
    assert "akb_" not in prompt["prompt"]
    wrong = client.post("/v1/onboarding/redeem", json={**body, "knowledge_base_id": str(uuid4())})
    assert wrong.status_code == 400
    response = client.post("/v1/onboarding/redeem", json=body)
    assert response.status_code == 200
    connection = response.json()
    assert client.post("/v1/onboarding/redeem", json=body).status_code == 400
    before = client.get("/v1/onboarding").json()
    assert not before["connected"] and before["answers"] == {}
    assert client.post("/v1/onboarding/start", json={"skill_version": SKILL_VERSION}).status_code == 403
    headers = agent(client, connection)
    assert client.post("/v1/onboarding/start", headers=headers, json={"skill_version": "old"}).status_code == 409
    connected = client.post("/v1/onboarding/start", headers=headers, json={"skill_version": SKILL_VERSION})
    assert connected.status_code == 200
    assert connected.json()["connected"] and connected.json()["next_question"]["key"] == "preferred_name"


def test_interview_resume_skip_revision_and_no_automatic_profile(app):
    _, client = app
    register(client)
    connection, _ = redeem(client)
    headers = agent(client, connection)
    start = client.post("/v1/onboarding/start", headers=headers, json={"skill_version": SKILL_VERSION}).json()
    response = client.patch("/v1/onboarding", headers=headers, json={
        "expected_revision": start["revision"], "skipped": ["preferred_name"], "answers": {"identity": "中学教师"}})
    assert response.status_code == 200
    current = response.json()
    assert current["next_question"]["key"] == "goal"
    assert client.patch("/v1/onboarding", headers=headers, json={"expected_revision": start["revision"], "answers": {"identity": "错误覆盖"}}).status_code == 409
    assert client.patch("/v1/onboarding", headers=headers, json={"expected_revision": current["revision"], "complete": True}).status_code == 422
    assert client.patch("/v1/onboarding", headers=headers, json={"expected_revision": current["revision"], "dismissed": True}).status_code == 403
    response = client.patch("/v1/onboarding", headers=headers, json={"expected_revision": current["revision"], "answers": {"goal": "准备教学申报材料"}, "complete": True})
    assert response.status_code == 200
    current = response.json()
    assert current["completed_at"] and current["next_question"] is None
    resumed = client.post("/v1/onboarding/start", headers=headers, json={"skill_version": SKILL_VERSION}).json()
    assert resumed["answers"] == current["answers"] and resumed["skipped"] == ["preferred_name"]
    profile = client.get("/v1/profile").json()
    assert profile["candidates"] == [] and all(f["value"] is None for f in profile["fields"].values())
    with TestClient(app[0]) as other:
        register(other)
        assert other.get("/v1/onboarding").json()["answers"] == {}
        assert other.get("/v1/onboarding", headers={**headers, "X-AKB-Knowledge-Base": other.get("/v1/context").json()["knowledge_base_id"]}).status_code == 409


def test_all_answers_can_complete_in_one_update_and_resume_without_questions(app):
    _, client = app
    register(client)
    connection, _ = redeem(client)
    headers = agent(client, connection)
    started = client.post('/v1/onboarding/start', headers=headers, json={'skill_version': SKILL_VERSION}).json()
    assert {q['key'] for q in started['pending_questions']} == {'preferred_name', 'identity', 'goal'}
    answers = {'preferred_name': '林老师', 'identity': '中学物理教师', 'goal': '整理教学材料，准备职称申报'}
    response = client.patch('/v1/onboarding', headers=headers, json={
        'expected_revision': started['revision'], 'answers': answers, 'complete': True})
    assert response.status_code == 200, response.text
    completed = response.json()
    assert completed['answers'] == answers and completed['completed_at']
    assert completed['pending_questions'] == [] and completed['next_question'] is None
    resumed = client.post('/v1/onboarding/start', headers=headers, json={'skill_version': SKILL_VERSION}).json()
    assert resumed['answers'] == answers and resumed['completed_at'] == completed['completed_at']
    assert resumed['pending_questions'] == []
    profile = client.get('/v1/profile').json()
    assert profile['candidates'] == [] and all(f['value'] is None for f in profile['fields'].values())


def test_expiry_regeneration_revocation_and_least_read_scope(app):
    application, client = app
    register(client)
    old, _ = ticket(client)
    fresh, _ = ticket(client)
    assert client.post("/v1/onboarding/redeem", json=old).status_code == 400
    with application.state.db.session(fresh["knowledge_base_id"]) as session:
        latest = session.scalar(select(ConnectionTicket).where(ConnectionTicket.used_at.is_(None)))
        latest.expires_at = now() - timedelta(seconds=1)
    assert client.post("/v1/onboarding/redeem", json=fresh).status_code == 400
    connection, _ = redeem(client)
    headers = agent(client, connection)
    assert client.post("/v1/onboarding/start", headers=headers, json={"skill_version": SKILL_VERSION}).status_code == 200
    assert client.delete("/v1/tokens/" + connection["credential_id"]).status_code == 204
    assert not client.get("/v1/onboarding").json()["connected"]
    limited = client.post("/v1/tokens", json={"label": "只读资料", "scopes": ["knowledge:read"]}).json()
    assert client.get("/v1/onboarding", headers={"Authorization": "Bearer " + limited["token"]}).status_code == 403
    body, _ = ticket(client)
    client.post("/v1/auth/logout")
    assert client.post("/v1/onboarding/redeem", json=body).status_code == 400


def installer_module(client, monkeypatch):
    spec = importlib.util.spec_from_file_location("zhixu_installer_test", ROOT / "scripts/install_workbuddy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def request(_base, path, body=None, token=None, kb=None):
        headers = {"Authorization": "Bearer " + token, "X-AKB-Knowledge-Base": kb} if token else {}
        response = client.request("POST" if body is not None else "GET", path, json=body, headers=headers)
        assert response.status_code < 300, response.text
        return response.json() if response.status_code != 204 else None
    monkeypatch.setattr(module, "request", request)
    return module


def test_installer_preserves_other_services_and_credential_free_receipt(app, monkeypatch, tmp_path):
    _, client = app
    register(client)
    module = installer_module(client, monkeypatch)
    target = tmp_path / ".workbuddy"
    target.mkdir()
    original = {"otherPreference": True, "mcpServers": {"other": {"command": "existing", "env": {"OTHER": "untouched"}}}}
    (target / "mcp.json").write_text(json.dumps(original))
    body, _ = ticket(client)
    receipt = module.install("http://127.0.0.1:5876", body["code"], body["knowledge_base_id"], target)
    config = json.loads((target / "mcp.json").read_text())
    assert config["mcpServers"]["other"] == original["mcpServers"]["other"] and config["otherPreference"]
    assert (target / "skills/personal-knowledge/references/onboarding.md").exists()
    assert receipt["status"] == "installed_waiting_for_mcp_selection" and "akb_" not in json.dumps(receipt)
    assert (target / "mcp.json").stat().st_mode & 0o777 == 0o600
    backups = list(target.glob("mcp.json.zhixu-backup-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == original
    assert not client.get("/v1/onboarding").json()["connected"]
    pending = client.get("/v1/onboarding").json()
    assert pending["installation"]["server_name"] == receipt["server_name"]
    revision = pending["revision"]
    before = (target / "mcp.json").read_bytes()
    status = module.inspect_installation("http://127.0.0.1:5876", body["knowledge_base_id"], target)
    assert status["status"] == "installed_waiting_for_mcp_selection"
    assert client.get("/v1/onboarding").json()["revision"] == revision
    assert (target / "mcp.json").read_bytes() == before


def test_installer_bad_config_does_not_consume_ticket_or_overwrite_skill(app, monkeypatch, tmp_path):
    application, client = app
    register(client)
    module = installer_module(client, monkeypatch)
    body, _ = ticket(client)
    (tmp_path / "mcp.json").write_text("broken-config")
    with pytest.raises(ValueError):
        module.install("http://127.0.0.1:5876", body["code"], body["knowledge_base_id"], tmp_path)
    assert (tmp_path / "mcp.json").read_text() == "broken-config"
    with application.state.db.session(body["knowledge_base_id"]) as session:
        assert session.scalar(select(ConnectionTicket).where(ConnectionTicket.used_at.is_(None)))
    (tmp_path / "mcp.json").write_text("{}")
    skill = tmp_path / "skills/personal-knowledge"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("user owned")
    with pytest.raises(ValueError, match="同名技能"):
        module.install("http://127.0.0.1:5876", body["code"], body["knowledge_base_id"], tmp_path)
    assert (skill / "SKILL.md").read_text() == "user owned"
    (skill / ".zhixu-managed").write_text("owned")
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {
        "zhixu-" + body["knowledge_base_id"]: {"command": "someone-else", "env": None}}}))
    original_config = (tmp_path / "mcp.json").read_bytes()
    with pytest.raises(ValueError, match="同名连接"):
        module.install("http://127.0.0.1:5876", body["code"], body["knowledge_base_id"], tmp_path)
    assert (tmp_path / "mcp.json").read_bytes() == original_config
    assert (skill / "SKILL.md").read_text() == "user owned"


def test_install_race_keeps_external_config_and_revokes_failed_credential(app, monkeypatch, tmp_path):
    application, client = app
    register(client)
    module = installer_module(client, monkeypatch)
    body, _ = ticket(client)
    (tmp_path / "mcp.json").write_text("{}")
    original_request = module.request
    issued = []

    def racing(*args, **kwargs):
        result = original_request(*args, **kwargs)
        if args[1] == "/v1/onboarding/redeem":
            issued.append(result["credential_id"])
            (tmp_path / "mcp.json").write_text('{"externalChange": true}')
        return result
    monkeypatch.setattr(module, "request", racing)
    with pytest.raises(ValueError, match="配置已变化"):
        module.install("http://127.0.0.1:5876", body["code"], body["knowledge_base_id"], tmp_path)
    assert json.loads((tmp_path / "mcp.json").read_text()) == {"externalChange": True}
    with application.state.db.session() as session:
        assert session.get(Credential, issued[0]).revoked
    assert not (tmp_path / "skills/personal-knowledge").exists()


def test_existing_directory_broker_compatibility_without_disabling_hooks(app, monkeypatch, tmp_path):
    _, client = app
    register(client)
    module = installer_module(client, monkeypatch)
    body, _ = ticket(client)
    (tmp_path / "skills").mkdir()
    mkdir = os.mkdir
    calls = []

    def brokered_mkdir(path, *args, **kwargs):
        calls.append(str(path))
        if Path(path).is_dir():
            raise PermissionError(errno.EEXIST, "simulated broker rejects existing-directory mkdir")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "mkdir", brokered_mkdir)
    result = module.install("http://127.0.0.1:5876", body["code"], body["knowledge_base_id"], tmp_path)
    assert result["status"] == "installed_waiting_for_mcp_selection"
    assert str(tmp_path) not in calls and str(tmp_path / "skills") not in calls
    # Leave the hook enabled for a managed-skill update as well.
    body, _ = ticket(client)
    module.install("http://127.0.0.1:5876", body["code"], body["knowledge_base_id"], tmp_path)


def test_real_permission_denial_stops_before_ticket_exchange(app, monkeypatch, tmp_path):
    application, client = app
    register(client)
    module = installer_module(client, monkeypatch)
    body, _ = ticket(client)
    target = tmp_path / "not-authorized"
    mkdir = os.mkdir

    def denied(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError(errno.EACCES, "simulated host permission required")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "mkdir", denied)
    with pytest.raises(PermissionError):
        module.install("http://127.0.0.1:5876", body["code"], body["knowledge_base_id"], target)
    with application.state.db.session(body["knowledge_base_id"]) as session:
        assert session.scalar(select(ConnectionTicket).where(ConnectionTicket.used_at.is_(None)))
    assert not target.exists()


def test_installation_receipt_is_not_connection_and_old_token_cannot_substitute(app):
    _, client = app
    register(client)
    first, _ = redeem(client)
    old = agent(client, first)
    client.post("/v1/onboarding/start", headers=old, json={"skill_version": SKILL_VERSION}).raise_for_status()
    second, _ = redeem(client)
    new = agent(client, second)
    response = client.post("/v1/onboarding/installation", headers=new, json={"skill_version": SKILL_VERSION})
    assert response.status_code == 200
    assert response.json()["installation"] and not response.json()["connected"]
    assert "credential_id" not in response.json()["installation"]
    assert client.post("/v1/onboarding/installation", json={"skill_version": SKILL_VERSION}).status_code == 403
    assert client.post("/v1/onboarding/start", headers=old, json={"skill_version": SKILL_VERSION}).status_code == 409
    assert client.post("/v1/onboarding/start", headers=new, json={"skill_version": SKILL_VERSION}).json()["connected"]
    client.delete("/v1/tokens/" + second["credential_id"]).raise_for_status()
    assert client.get("/v1/onboarding").json()["installation"] is None


def test_failed_install_report_restores_previous_config_skill_and_receipt(app, monkeypatch, tmp_path):
    application, client = app
    register(client)
    module = installer_module(client, monkeypatch)
    first, _ = ticket(client)
    receipt = module.install("http://127.0.0.1:5876", first["code"], first["knowledge_base_id"], tmp_path)
    before = (tmp_path / "mcp.json").read_bytes()
    receipt_path = tmp_path / (receipt["server_name"] + ".installation.json")
    previous_receipt = receipt_path.read_bytes()
    marker = tmp_path / "skills/personal-knowledge/local-user-note.txt"
    marker.write_text("preserve this")
    original_request = module.request
    issued = []

    def lose_report(*args, **kwargs):
        result = original_request(*args, **kwargs)
        if args[1] == "/v1/onboarding/redeem":
            issued.append(result["credential_id"])
        if args[1] == "/v1/onboarding/installation":
            raise OSError("simulate lost response after server commit")
        return result

    monkeypatch.setattr(module, "request", lose_report)
    body, _ = ticket(client)
    with pytest.raises(OSError):
        module.install("http://127.0.0.1:5876", body["code"], body["knowledge_base_id"], tmp_path)
    assert (tmp_path / "mcp.json").read_bytes() == before
    assert receipt_path.read_bytes() == previous_receipt
    assert marker.read_text() == "preserve this"
    with application.state.db.session() as session:
        assert session.get(Credential, issued[0]).revoked
