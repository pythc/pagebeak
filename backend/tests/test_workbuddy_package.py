import importlib.util
import io
import json
import os
from urllib.parse import parse_qs, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from akb import workbuddy_entry as entry
from akb.config import ROOT
from akb.models import Credential
from akb.security import digest_token
from akb.workbuddy_package import published_install_link
from conftest import register
from sqlalchemy import select


def package(client, path):
    response = client.post("/v1/onboarding/package")
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    path.write_bytes(response.content)
    with ZipFile(io.BytesIO(response.content)) as archive:
        files = {p: archive.read(p) for p in archive.namelist()}
    name = next(iter(files)).split("/")[0]
    config = json.loads(files[name + "/.mcp.json"])["mcpServers"][name]
    return name, config, files


def requests_for(client, monkeypatch):
    def request(base, path, body=None, token=None, kb=None):
        headers = {"Authorization": "Bearer " + token, "X-AKB-Knowledge-Base": kb} if token else {}
        response = client.request("POST" if body is not None else "GET", path, json=body, headers=headers)
        if response.status_code in {401, 403}:
            raise entry.AuthorizationExpired("expired")
        if response.status_code >= 300:
            raise entry.ConnectionError("request failed")
        return response.json() if response.status_code != 204 else None
    monkeypatch.setattr(entry, "request", request)
    return request


def installer():
    spec = importlib.util.spec_from_file_location("expert_installer", ROOT / "scripts/install_workbuddy_package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_download_does_not_install_and_is_tenant_bound(app, tmp_path):
    _, client = app
    register(client)
    name, config, files = package(client, tmp_path / "package.zip")
    state = client.get("/v1/onboarding").json()
    assert not state["connected"] and state["installation"] is None
    assert not state["distribution"]["one_click_ready"]
    kb = state["knowledge_base_id"]
    assert config["env"]["AKB_EXPECTED_KB_ID"] == kb and name.endswith("-expert")
    assert "AKB_TOKEN" not in config["env"]
    manifest = json.loads(files[name + "/.codebuddy-plugin/plugin.json"])
    start_query = parse_qs(urlsplit(state["distribution"]["initialization_url"]).query)
    assert start_query["expertId"] == [name] and start_query["permissionMode"] == ["default"]
    assert start_query["cwd"] == [str(ROOT)] and kb in start_query["prompt"][0]
    assert start_query["prompt"][0].startswith(manifest["defaultInitPrompt"]["zh"])
    assert "join_" not in state["distribution"]["initialization_url"]
    # The persistent entry survives page refresh without issuing or consuming a connection code.
    assert client.get("/v1/onboarding").json()["distribution"] == state["distribution"]
    assert "dependencies" not in manifest and name + "/.mcp.json" in files
    assert manifest["defaultInitPrompt"] == manifest["quickPrompts"][0]
    for path in manifest["agents"] + [p + "/SKILL.md" for p in manifest["skills"]]:
        assert name + "/" + path.removeprefix("./") in files
    assert kb.encode() in files[name + "/agents/knowledge-steward.md"]
    api_token = client.post("/v1/tokens", json={"label": "test", "scopes": ["knowledge:read"]}).json()["token"]
    assert client.post("/v1/onboarding/package", headers={"Authorization": "Bearer " + api_token}).status_code == 403
    csrf = client.headers.pop("x-csrf-token")
    assert client.post("/v1/onboarding/package").status_code == 403
    client.headers["X-CSRF-Token"] = csrf


def test_launch_contains_no_access_code_and_keeps_private_package(app):
    application, client = app
    register(client)
    response = client.post("/v1/onboarding/package/launch")
    assert response.status_code == 201 and "no-store" in response.headers["cache-control"]
    result = response.json()
    url = urlsplit(result["launch_url"])
    assert url.scheme == "workbuddy" and url.netloc == "task"
    query = parse_qs(url.query)
    assert query["action"] == ["start"] and result["knowledge_base_id"] in query["prompt"][0]
    assert query["cwd"] == [str(ROOT)] and query["permissionMode"] == ["default"]
    assert "join_" not in result["launch_url"] and "fullAccess" not in result["launch_url"]
    assert len(result["launch_url"]) < 6000
    archives = list(application.state.settings.workbuddy_package_dir.rglob("*.zip"))
    assert len(archives) == 1 and archives[0].stat().st_mode & 0o777 == 0o600
    assert not client.get("/v1/onboarding").json()["connected"]


def test_first_boot_repeat_revocation_and_renewal(app, monkeypatch, tmp_path):
    application, client = app
    register(client)
    request = requests_for(client, monkeypatch)
    name, config, _ = package(client, tmp_path / "package.zip")
    env = config["env"]
    directory = tmp_path / "private"
    token = entry.bind(env["AKB_API_URL"], env["AKB_EXPECTED_KB_ID"], env["AKB_JOIN_CODE"], directory)
    saved = directory / (env["AKB_EXPECTED_KB_ID"] + ".json")
    assert saved.stat().st_mode & 0o777 == 0o600 and directory.stat().st_mode & 0o777 == 0o700
    state = client.get("/v1/onboarding").json()
    assert state["installation"]["channel"] == "expert" and state["installation"]["server_name"] == name
    assert not state["connected"]
    assert entry.bind(env["AKB_API_URL"], env["AKB_EXPECTED_KB_ID"], "expired-code", directory) == token
    with application.state.db.session() as session:
        credential_id = session.scalar(select(Credential).where(Credential.digest == digest_token(token))).id
    assert client.delete("/v1/tokens/" + credential_id).status_code == 204
    before = saved.read_bytes()
    with pytest.raises(entry.AuthorizationExpired):
        entry.bind(env["AKB_API_URL"], env["AKB_EXPECTED_KB_ID"], "", directory)
    assert saved.read_bytes() == before
    _, fresh, _ = package(client, tmp_path / "fresh.zip")
    fresh_env = fresh["env"]
    # A temporary network error must not spend the fresh ticket.
    def unavailable(*args, **kwargs):
        raise entry.ConnectionError("offline")
    monkeypatch.setattr(entry, "request", unavailable)
    with pytest.raises(entry.ConnectionError):
        entry.bind(fresh_env["AKB_API_URL"], fresh_env["AKB_EXPECTED_KB_ID"], fresh_env["AKB_JOIN_CODE"], directory)
    assert saved.read_bytes() == before
    monkeypatch.setattr(entry, "request", request)
    replacement = entry.bind(fresh_env["AKB_API_URL"], fresh_env["AKB_EXPECTED_KB_ID"], fresh_env["AKB_JOIN_CODE"], directory)
    assert replacement != token
    assert not client.get("/v1/onboarding").json()["connected"]


def test_failed_private_write_revokes_new_credential(app, monkeypatch, tmp_path):
    application, client = app
    register(client)
    requests_for(client, monkeypatch)
    _, config, _ = package(client, tmp_path / "package.zip")
    env = config["env"]
    def denied(*args):
        raise PermissionError("denied")
    monkeypatch.setattr(entry, "write_private", denied)
    with pytest.raises(PermissionError):
        entry.bind(env["AKB_API_URL"], env["AKB_EXPECTED_KB_ID"], env["AKB_JOIN_CODE"], tmp_path / "private")
    with application.state.db.session(env["AKB_EXPECTED_KB_ID"]) as session:
        from akb.models import User
        user = session.scalar(select(User).where(User.tenant_id == env["AKB_EXPECTED_KB_ID"]))
        issued = session.scalars(select(Credential).where(Credential.kind == "api", Credential.user_id == user.id)).all()
        assert len(issued) == 1 and issued[0].revoked


def test_native_registration_preserves_existing_and_does_not_approve(app, monkeypatch, tmp_path):
    _, client = app
    register(client)
    archive = tmp_path / "package.zip"
    name, _, _ = package(client, archive)
    module = installer()
    host = tmp_path / "host"
    host.mkdir()
    (host / "mcp.json").write_text('{"other":"kept"}')
    (host / "mcp-approvals.json").write_text('{"other":"trusted"}')
    registry = host / "plugins/marketplaces/my-experts/.codebuddy-plugin/marketplace.json"
    registry.parent.mkdir(parents=True)
    original = {"name": "my-experts", "custom": True, "plugins": [{"name": "other", "source": "./plugins/other"}]}
    registry.write_text(json.dumps(original))
    monkeypatch.delenv("CODEBUDDY_SESSION_ID", raising=False)
    receipt = module.install(archive, host)
    assert receipt["status"] == "registered_waiting_for_host" and not receipt["session_marker_present"]
    actual = json.loads(registry.read_text())
    assert actual["custom"] and actual["plugins"][0] == original["plugins"][0]
    assert actual["plugins"][1]["name"] == name
    assert (host / "mcp.json").read_text() == '{"other":"kept"}'
    assert (host / "mcp-approvals.json").read_text() == '{"other":"trusted"}'
    assert module.install(archive, host)["status"] == "registered_waiting_for_host"
    assert json.loads(registry.read_text()) == actual
    # A refreshed short-lived code can be installed, but a user's edits must never be overwritten.
    package(client, archive)
    assert module.install(archive, host)["status"] == "registered_waiting_for_host"
    agent_file = registry.parent.parent / "plugins" / name / "agents/knowledge-steward.md"
    agent_file.write_text("human change")
    with pytest.raises(ValueError, match="已修改"):
        module.install(archive, host)
    assert agent_file.read_text() == "human change"


def test_registration_failure_restores_registry_and_rejects_tampered_package(app, monkeypatch, tmp_path):
    _, client = app
    register(client)
    archive = tmp_path / "package.zip"
    name, _, files = package(client, archive)
    module = installer()
    host = tmp_path / "host"
    registry = host / "plugins/marketplaces/my-experts/.codebuddy-plugin/marketplace.json"
    registry.parent.mkdir(parents=True)
    registry.write_text('{"name":"my-experts","plugins":[]}')
    before = registry.read_bytes()
    replace = os.replace
    def fail_after_write(src, dst):
        replace(src, dst)
        if str(src).find(".zhixu-registry-") >= 0:
            raise OSError("after commit")
    monkeypatch.setattr(module.os, "replace", fail_after_write)
    with pytest.raises(OSError):
        module.install(archive, host)
    assert registry.read_bytes() == before
    assert not (registry.parent.parent / "plugins" / name).exists()
    altered = tmp_path / "altered.zip"
    with ZipFile(altered, "w", ZIP_DEFLATED) as z:
        for path, value in files.items():
            z.writestr(path, value)
        z.writestr(name + "/hooks/extra.sh", "exit 0")
    with pytest.raises(ValueError, match="不一致"):
        module.inspect_package(altered)
    assert registry.read_bytes() == before


def test_published_link_uses_only_explicit_asset_identity():
    with pytest.raises(ValueError):
        published_install_link("知序", "")
    url = urlsplit(published_install_link("知序 & 管家", "published-example"))
    assert url.netloc == "codebuddy-ide" and url.path == "/expert/install"
    assert parse_qs(url.query) == {"expertname": ["知序 & 管家"], "sharecode": ["published-example"]}


def test_refresh_failure_restores_existing_expert_and_code(app, monkeypatch, tmp_path):
    _, client = app
    register(client)
    archive = tmp_path / "package.zip"
    name, _, _ = package(client, archive)
    module = installer()
    host = tmp_path / "host"
    monkeypatch.delenv("CODEBUDDY_SESSION_ID", raising=False)
    module.install(archive, host)
    market = host / "plugins/marketplaces/my-experts"
    destination = market / "plugins" / name
    registry = market / ".codebuddy-plugin/marketplace.json"
    original_registry = registry.read_bytes()
    original_files = {p.relative_to(destination): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
    package(client, archive)
    replace = os.replace

    def fail_after_write(src, dst):
        replace(src, dst)
        if ".zhixu-registry-" in str(src):
            raise OSError("simulated refresh failure")

    monkeypatch.setattr(module.os, "replace", fail_after_write)
    with pytest.raises(OSError):
        module.install(archive, host)
    assert registry.read_bytes() == original_registry
    assert {p.relative_to(destination): p.read_bytes() for p in destination.rglob("*") if p.is_file()} == original_files
