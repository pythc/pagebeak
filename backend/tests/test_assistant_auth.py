import asyncio
import base64
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zipfile import ZipFile

import pytest
from akb import mcp_server, native, native_start
from akb.api import create_app
from akb.assistant_auth import public_origin
from akb.assistant_package import NAME, build_assistant, connection_name
from akb.models import AssistantGrant, Credential, User, now
from akb.security import SCOPES, Principal, digest_token
from conftest import register
from fastapi.testclient import TestClient
from sqlalchemy import select


@pytest.fixture
def assistant(test_settings, tmp_path, monkeypatch, request):
    settings = test_settings.model_copy(update={"knowledge_engine": getattr(request, "param", "tencent"), "storage_dir": tmp_path,
        "assistant_public_origin": "http://127.0.0.1:5876", "native_origin": "http://localhost:5878"})
    application = create_app(settings)
    owners = {}
    monkeypatch.setattr(native, "verify_identity", lambda settings, token: {"id": token})
    monkeypatch.setattr(native_start, "principal", lambda db, identity, **kwargs: owners[identity["id"]])
    with TestClient(application, base_url=settings.assistant_public_origin) as client:
        yield application, client, owners


def owner(application, client, owners, name="合成教师"):
    data = register(client, name)
    with application.state.db.session() as session:
        user = session.get(User, data["id"])
        p = Principal(user.id, user.tenant_id, user.display_name, "", set(SCOPES), "browser", "")
    identity = uuid4().hex
    owners[identity] = p
    return p, {"X-Cloudreve-Token": identity, "X-AKB-File-User": identity}


def registered(client):
    callback = "workbuddy://workbuddy/mcp/connector%3Azhixu-teacher-assistant/oauth/callback"
    result = client.post("/register", json={"redirect_uris": [callback], "client_name": "WorkBuddy 本地测试",
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"]})
    assert result.status_code == 201, result.text
    data = result.json()
    assert data["redirect_uris"] == [callback] and not data.get("client_secret")
    return data["client_id"], callback


def pending(client, client_id, callback):
    verifier = "x" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    response = client.get("/authorize", params={"client_id": client_id, "redirect_uri": callback,
        "response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256", "state": "opaque-state",
        "resource": "http://127.0.0.1:5876/mcp"}, follow_redirects=False)
    assert response.status_code == 302, response.text
    reference = parse_qs(urlsplit(response.headers["location"]).query)["assistant_request"][0]
    return reference, verifier


def consent(client, headers, reference):
    result = client.get("/v1/native/assistant/authorization", params={"reference": reference}, headers=headers)
    assert result.status_code == 200, result.text
    result = client.post("/v1/native/assistant/authorization", headers=headers,
        json={"reference": reference, "nonce": result.json()["nonce"], "allowed": True})
    assert result.status_code == 200, result.text
    query = parse_qs(urlsplit(result.json()["redirect_url"]).query)
    assert query["state"] == ["opaque-state"]
    return query["code"][0]


def exchange(client, client_id, callback, code, verifier):
    return client.post("/token", data={"grant_type": "authorization_code", "client_id": client_id,
        "redirect_uri": callback, "code": code, "code_verifier": verifier})


def test_real_oauth_pkce_single_use_refresh_and_revocation(assistant):
    app, client, owners = assistant
    p, headers = owner(app, client, owners)
    cid, callback = registered(client)
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["issuer"] == "http://127.0.0.1:5876/"
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == "http://127.0.0.1:5876/mcp"
    reference, verifier = pending(client, cid, callback)
    code = consent(client, headers, reference)
    assert exchange(client, cid, callback, code, "wrong" * 10).status_code == 400
    response = exchange(client, cid, callback, code, verifier)
    assert response.status_code == 200, response.text
    tokens = response.json()
    assert tokens["expires_in"] == 3600
    assert exchange(client, cid, callback, code, verifier).status_code == 400
    provider = app.state.assistant_authorization
    access = asyncio.run(provider.load_access_token(tokens["access_token"]))
    assert access.claims["knowledge_base_id"] == p.tenant_id
    with app.state.db.session() as session:
        credential = session.scalar(select(Credential).where(Credential.digest == digest_token(tokens["access_token"])))
        credential_id = credential.id
    old = client.post("/v1/tokens", json={"label": "existing local connector", "scopes": sorted(SCOPES)}).json()["token"]
    assert client.post("/v1/onboarding/installation", json={"skill_version": "2026.10.17.1", "channel": "expert"},
        headers={"Authorization": "Bearer " + old}).status_code == 200
    started = client.post("/v1/onboarding/start", json={"skill_version": "2026.10.17.1"},
        headers={"Authorization": "Bearer " + tokens["access_token"]})
    assert started.status_code == 200, started.text
    assert started.json()["connected"] and started.json()["installation"]["server_name"] == NAME
    assert started.json()["distribution"]["mode"] == "remote_preview"
    answers = {"preferred_name": "合成教师", "identity": "合成测试身份", "goal": "整理测试资料"}
    saved = client.patch("/v1/onboarding", headers={"Authorization": "Bearer " + tokens["access_token"]},
        json={"expected_revision": started.json()["revision"], "answers": answers, "complete": True})
    assert saved.status_code == 200, saved.text
    refreshed = client.post("/token", data={"grant_type": "refresh_token", "client_id": cid,
        "refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    fresh = refreshed.json()
    resumed = client.get("/v1/onboarding", headers={"Authorization": "Bearer " + fresh["access_token"]}).json()
    assert resumed["connected"] and resumed["completed_at"] and resumed["answers"] == answers
    assert fresh["access_token"] != tokens["access_token"] and fresh["refresh_token"] != tokens["refresh_token"]
    assert asyncio.run(provider.load_access_token(tokens["access_token"])) is None
    with app.state.db.session() as session:
        assert session.scalar(select(Credential).where(Credential.digest == digest_token(fresh["access_token"]))).id == credential_id
    assert client.post("/token", data={"grant_type": "refresh_token", "client_id": cid,
        "refresh_token": tokens["refresh_token"]}).status_code == 400
    revoked = client.post("/revoke", data={"client_id": cid, "token": fresh["refresh_token"], "token_type_hint": "refresh_token"})
    assert revoked.status_code == 200, revoked.text
    assert asyncio.run(provider.load_access_token(fresh["access_token"])) is None
    assert client.post("/token", data={"grant_type": "refresh_token", "client_id": cid,
        "refresh_token": fresh["refresh_token"]}).status_code == 400


def test_consent_identity_nonce_expiry_redirect_and_scope_boundaries(assistant):
    app, client, owners = assistant
    start_headers = {"X-Cloudreve-Token": "unlinked-drive-account-123", "X-AKB-File-User": "unlinked-drive-account-123"}
    unlinked = client.get("/v1/native/start", headers=start_headers)
    assert unlinked.status_code == 200 and unlinked.json()["assistant_mode"] == "remote_preview"
    assert not unlinked.json()["linked"]
    _, first = owner(app, client, owners)
    _, second = owner(app, client, owners, "另一位教师")
    cid, callback = registered(client)
    reference, _ = pending(client, cid, callback)
    description = client.get("/v1/native/assistant/authorization", params={"reference": reference}, headers=first).json()
    body = {"reference": reference, "nonce": description["nonce"], "allowed": True}
    assert client.post("/v1/native/assistant/authorization", json=body, headers=second).status_code == 409
    assert client.post("/v1/native/assistant/authorization", json={**body, "nonce": "y" * 43}, headers=first).status_code == 409
    assert client.post("/v1/native/assistant/authorization", json=body,
                       headers={**first, "Origin": "https://untrusted.example"}).status_code == 403
    denied = client.post("/v1/native/assistant/authorization", json={**body, "allowed": False}, headers=first)
    assert "access_denied" in denied.json()["redirect_url"]
    assert client.post("/v1/native/assistant/authorization", json=body, headers=first).status_code == 410
    reference, _ = pending(client, cid, callback)
    with app.state.db.session() as session:
        row = session.scalar(select(AssistantGrant).where(AssistantGrant.digest == digest_token(reference)))
        row.expires_at = now() - timedelta(seconds=1)
    assert client.get("/v1/native/assistant/authorization", params={"reference": reference}, headers=first).status_code == 410
    for redirect in ["http://untrusted.example/callback", "javascript:alert(1)"]:
        result = client.post("/register", json={"redirect_uris": [redirect], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        assert result.status_code == 400


@pytest.mark.parametrize("assistant", ["tencent", "weknora"], indirect=True)
def test_remote_mcp_never_exposes_server_disk_and_binds_each_user(assistant, monkeypatch):
    app, client, owners = assistant
    tokens = []
    for name in ["甲教师", "乙教师"]:
        p, headers = owner(app, client, owners, name)
        cid, callback = registered(client)
        reference, verifier = pending(client, cid, callback)
        code = consent(client, headers, reference)
        token = exchange(client, cid, callback, code, verifier).json()["access_token"]
        tokens.append((p, token))

    def api(method, path, body=None, key=None, **kwargs):
        current = mcp_server.connection()
        result = client.request(method, path, json=body,
            headers={"Authorization": "Bearer " + current["token"], "X-AKB-Knowledge-Base": current["expected"]})
        assert result.status_code == 200, result.text
        return result.json()

    monkeypatch.setattr(mcp_server, "call", api)
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    for path in ("/mcp", "/mcp/"):
        unauthenticated = client.post(path, headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, follow_redirects=False)
        assert unauthenticated.status_code == 401
        assert "location" not in unauthenticated.headers
        assert "resource_metadata" in unauthenticated.headers["www-authenticate"]
    def query(pair):
        p, token = pair
        request_headers = {**headers, "Authorization": "Bearer " + token}
        listed = client.post("/mcp", headers=request_headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, follow_redirects=False)
        assert listed.status_code == 200, listed.text
        assert "location" not in listed.headers
        names = {tool["name"] for tool in listed.json()["result"]["tools"]}
        assert {"get_knowledge_context", "prepare_filling", "read_assistant_skill", "get_upload_entry"} <= names
        assert not {"upload_file", "configure_local_documents", "read_local_document"} & names
        external_jobs = {"list_organization_jobs", "claim_organization_job", "renew_organization_job", "complete_organization_job", "release_organization_job"}
        if app.state.settings.knowledge_engine == "weknora":
            assert external_jobs <= names
            jobs = client.post("/mcp/", headers=request_headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "list_organization_jobs", "arguments": {}}})
            assert jobs.status_code == 200 and not jobs.json()["result"].get("isError"), jobs.text
        else:
            assert not external_jobs & names
        result = client.post("/mcp/", headers=request_headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_knowledge_context", "arguments": {"expected_knowledge_base_id": p.tenant_id,
                "expected_service_origin": app.state.settings.assistant_public_origin}}})
        assert result.status_code == 200, result.text
        assert not result.json()["result"].get("isError"), result.text
        payload = result.json()["result"]
        data = payload.get("structuredContent") or json.loads(payload["content"][0]["text"])
        assert data["knowledge_base_id"] == p.tenant_id
        assert data["service_origin"] == app.state.settings.assistant_public_origin
        assert data["connection_state"] == {"status": "authorized", "reauthorization_required": False,
                                            "next_action": "read_skill_and_resume_onboarding"}
        if app.state.settings.knowledge_engine == "weknora":
            # This client deliberately has no WorkBuddy-specific connection tool.
            # Its existing OAuth grant is sufficient to read the new guide and
            # start onboarding; no second consent or local scan is performed.
            guide_result = client.post("/mcp/", headers=request_headers,
                json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
                    "name": "read_assistant_skill", "arguments": {"reference": "references/connection.md"}}})
            assert guide_result.status_code == 200 and not guide_result.json()["result"].get("isError")
            guide_payload = guide_result.json()["result"]
            guide = guide_payload.get("structuredContent") or json.loads(guide_payload["content"][0]["text"])
            assert guide["version"] == "0.13.9" and '直接读取最新技能' in guide["text"]
            for tool, arguments in [("start_onboarding", {"skill_version": "2026.10.31.1"}), ("read_onboarding", {})]:
                initialized = client.post("/mcp/", headers=request_headers,
                    json={"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
                assert initialized.status_code == 200 and not initialized.json()["result"].get("isError"), initialized.text
                initialized_payload = initialized.json()["result"]
                state = initialized_payload.get("structuredContent") or json.loads(initialized_payload["content"][0]["text"])
                assert state["connected"] and state["knowledge_base_id"] == p.tenant_id
                assert state["next_question"]["key"] == "local_documents" and state["source_count"] == 0
                assert {option["value"] for option in state["local_document_options"]} == {"all", "folders", "none"}
        wrong = client.post("/mcp/", headers=request_headers,
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "get_knowledge_context", "arguments": {
                    "expected_service_origin": "https://different-campus.example"}}})
        assert wrong.json()["result"]["isError"]
        assert "knowledge_base_id" not in str(wrong.json()["result"])
        assert data["local_documents"]["available"] == (app.state.settings.knowledge_engine == "weknora")
        if data["local_documents"]["available"]:
            assert not data["local_documents"]["remote_path_access"]
            assert {"prepare_host_file_upload", "prepare_host_catalog_sync", "read_shared_local_catalog"} <= names
        assert mcp_server.request_connection.get() is None
    # The same HTTP process must keep two simultaneous authenticated requests separate.
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(query, tokens))


def test_public_package_contains_no_personal_binding_and_rejects_unsafe_origin():
    from akb.config import ROOT

    avatar = ROOT / "integrations/workbuddy/assets/zhixu-512.png"
    expert, connector = build_assistant("https://documents.example.test", "developer@example.test", avatar.read_bytes())
    assert (expert, connector) == build_assistant("https://documents.example.test", "developer@example.test", avatar.read_bytes())
    for payload in [expert, connector]:
        with ZipFile(io.BytesIO(payload)) as bundle:
            content = b"\n".join(bundle.read(n) for n in bundle.namelist() if n.endswith((".json", ".md")))
            assert not any(value in content for value in [b"join_", b"AKB_TOKEN", b"/Users/alex", b"sys.executable"])
    with ZipFile(io.BytesIO(expert)) as bundle:
        config = json.loads(bundle.read(NAME + "/.mcp.json"))["mcpServers"][connection_name("https://documents.example.test")]
        assert config["x-workbuddy"]["auth"]["type"] == "oauth"
        manifest = json.loads(bundle.read(NAME + "/.codebuddy-plugin/plugin.json"))
        assert manifest["defaultInitPrompt"] == manifest["quickPrompts"][0]
        assert 40 <= len(manifest["displayDescription"]["zh"]) <= 50
    for url in ["http://10.0.0.1", "https://host.example/other", "https://user:pass@host.example", "https://host.example?key=x"]:
        with pytest.raises(ValueError):
            public_origin(url)


def test_weknora_assistant_contains_external_executor_workflow():
    from akb.config import ROOT
    expert, connector = build_assistant("https://documents.example.test", "developer@example.test",
        (ROOT / "integrations/workbuddy/assets/zhixu-512.png").read_bytes(), engine="weknora")
    for payload, manifest_path in ((expert, ".codebuddy-plugin/plugin.json"), (connector, "connector-meta.json")):
        with ZipFile(io.BytesIO(payload)) as bundle:
            manifest = json.loads(bundle.read(NAME + "/" + manifest_path))
            assert manifest["version"] == "0.13.9"
            skill = bundle.read(NAME + "/skills/personal-knowledge/SKILL.md").decode()
            assert "claim_organization_job" in skill and "complete_organization_job" in skill
            assert "CPU" in skill and "WeKnora" in skill
            assert NAME + "/skills/personal-knowledge/scripts/host_documents.py" in bundle.namelist()
            assert NAME + "/skills/personal-knowledge/scripts/pypdf.zip" in bundle.namelist()


def test_assistant_connections_separate_service_origins_and_keep_prompts_consistent():
    from akb.assistant_package import initialization_link, skill_texts
    from akb.config import ROOT

    avatar = (ROOT / "integrations/workbuddy/assets/zhixu-512.png").read_bytes()
    origins = ["http://127.0.0.1:5890", "https://documents.example.test"]
    assert len({connection_name(origin) for origin in origins}) == 2
    for origin in origins:
        key = connection_name(origin)
        expert, connector = build_assistant(origin, "developer@example.test", avatar, engine="weknora")
        for payload, path in [(expert, ".mcp.json"), (connector, "mcp.json")]:
            with ZipFile(io.BytesIO(payload)) as bundle:
                servers = json.loads(bundle.read(NAME + "/" + path))["mcpServers"]
                assert list(servers) == [key]
                assert servers[key]["url"] == origin + "/mcp"
                assert key in bundle.read(NAME + "/skills/personal-knowledge/SKILL.md").decode()
        with ZipFile(io.BytesIO(expert)) as bundle:
            assert key in bundle.read(NAME + "/agents/teacher-assistant.md").decode()
        assert key in skill_texts("weknora", origin)["SKILL.md"]
        link = initialization_link("2b9bdeaa-4abb-4138-b1f3-56770df11adb", origin)
        assert key in parse_qs(urlsplit(link).query)["prompt"][0]


@pytest.mark.parametrize("assistant", ["weknora"], indirect=True)
def test_shared_install_download_has_no_personal_credential_or_server_path(assistant):
    app, client, owners = assistant
    p, headers = owner(app, client, owners)
    assert client.get("/v1/assistant/package").status_code == 404
    assert client.post("/v1/native/start/launch", headers=headers).status_code == 409
    app.state.settings.assistant_author_email = "developer@example.test"
    with app.state.db.session(p.tenant_id) as session:
        before = set(session.scalars(select(Credential.id)))
    launched = client.post("/v1/native/start/launch", headers=headers)
    assert launched.status_code == 201, launched.text
    data = launched.json()
    query = parse_qs(urlsplit(data["launch_url"]).query)
    assert query["prompt"] == [data["prompt"]]
    assert p.tenant_id not in data["prompt"] and "/v1/assistant/package" in data["prompt"]
    payload = client.get("/v1/assistant/package")
    assert payload.status_code == 200
    assert hashlib.sha256(payload.content).hexdigest() == data["sha256"]
    assert payload.headers["etag"] == '"' + data["sha256"] + '"'
    assert client.post("/v1/native/start/package", headers=headers).content == payload.content
    with ZipFile(io.BytesIO(payload.content)) as bundle:
        content = b"\n".join(bundle.read(n) for n in bundle.namelist() if n.endswith((".json", ".md")))
        assert not any(value in content for value in [p.tenant_id.encode(), b"join_", b"AKB_TOKEN", b"/Users/alex", b"/app/"])
        mcp = json.loads(bundle.read(NAME + "/.mcp.json"))["mcpServers"][connection_name(app.state.settings.assistant_public_origin)]
        assert "headers" not in mcp and mcp["x-workbuddy"]["auth"]["type"] == "oauth"
    with app.state.db.session(p.tenant_id) as session:
        assert set(session.scalars(select(Credential.id))) == before
    assert client.post("/v1/native/start/launch", headers={**headers, "Origin": "https://foreign.example"}).status_code == 403


def test_empty_legacy_broker_override_disables_storage_account(monkeypatch):
    from akb.config import Settings

    monkeypatch.setenv("AKB_CLOUDREVE_CREDENTIALS", "")
    assert Settings().cloudreve_credentials is None


def test_origin_change_does_not_relabel_existing_oauth_tokens(assistant):
    app,client,owners=assistant
    _,headers=owner(app,client,owners)
    cid,callback=registered(client)
    reference,verifier=pending(client,cid,callback)
    tokens=exchange(client,cid,callback,consent(client,headers,reference),verifier).json()
    provider=app.state.assistant_authorization
    provider.resource='https://new-campus.example/mcp'
    assert asyncio.run(provider.load_access_token(tokens['access_token'])) is None
    registered_client=asyncio.run(provider.get_client(cid))
    assert asyncio.run(provider.load_refresh_token(registered_client,tokens['refresh_token'])) is None
