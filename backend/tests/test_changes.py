import hashlib
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from akb import native
from akb.models import CatalogEntry, Job, NativeConnection, Source, User, now
from conftest import drain, register, upload
from sqlalchemy import select


def tenant(app, user):
    with app.state.db.session() as session:
        return session.get(User, user["id"]).tenant_id


def test_hash_checkpoint_history_and_actor_are_durable(app, worker):
    _, client = app
    register(client)
    source_id = upload(client)["source"]["id"]
    before = client.get("/v1/sync/state").json()
    assert before["needs_update"]
    drain(worker)
    done = client.get("/v1/sync/state").json()
    assert not done["needs_update"] and done["current_hash"] == done["indexed_hash"]
    assert client.patch(f"/v1/files/{source_id}", json={"name": "新名称.txt"}).status_code == 200
    assert client.get("/v1/sync/state").json()["needs_update"]
    drain(worker)
    assert not client.get("/v1/sync/state").json()["needs_update"]
    log = client.get("/v1/changes?limit=1").json()
    assert log["has_more"] and log["items"][0]["actor"] == "browser"
    following = client.get("/v1/changes", params={"after": log["next_cursor"]}).json()
    assert following["items"][0]["action"] == "rename"
    assert following["items"][0]["before"]["name"] == "简历.txt"
    assert following["items"][0]["after"]["name"] == "新名称.txt"
    assert client.patch(f"/v1/files/{source_id}", json={"name": "简历.txt"}).status_code == 200
    drain(worker)
    # A→B→A returns to the same state hash, but never loses the intervening history.
    reverted = client.get("/v1/sync/state").json()
    assert reverted["current_hash"] == done["current_hash"]
    assert reverted["latest_change_id"] > done["latest_change_id"]


def test_failed_parse_does_not_acknowledge_a_version(app, worker):
    _, client = app
    register(client)
    client.post(
        "/v1/sources/upload",
        files={"file": ("未知.bin", b"not-supported")},
        data={"subject_scope": "reference"},
    )
    drain(worker)
    state = client.get("/v1/sync/state").json()
    assert state["needs_update"] and state["files"][0]["indexed_hash"] == ""


def test_agent_checkpoint_survives_background_updates_and_is_per_credential(app, worker):
    _, client = app
    register(client)
    source_id = upload(client)["source"]["id"]
    drain(worker)
    first = client.post("/v1/tokens", json={"label": "agent-one", "scopes": ["knowledge:read"]}).json()[
        "token"
    ]
    second = client.post("/v1/tokens", json={"label": "agent-two", "scopes": ["knowledge:read"]}).json()[
        "token"
    ]
    headers = {"Authorization": "Bearer " + first}
    state = client.get("/v1/sync/state", headers=headers).json()
    assert not state["needs_update"] and state["agent_needs_review"]
    ack = {"expected_hash": state["current_hash"], "through_change_id": state["latest_change_id"]}
    assert client.post("/v1/changes/acknowledge", headers=headers, json=ack).status_code == 200
    assert not client.get("/v1/sync/state", headers=headers).json()["agent_needs_review"]
    assert client.get("/v1/sync/state", headers={"Authorization": "Bearer " + second}).json()[
        "agent_needs_review"
    ]
    client.patch(f"/v1/files/{source_id}", json={"name": "用户后来改名.txt"})
    drain(worker)
    changed = client.get("/v1/sync/state", headers=headers).json()
    assert not changed["needs_update"] and changed["agent_needs_review"]
    assert changed["agent_observed_hash"] == state["current_hash"]
    assert client.post("/v1/changes/acknowledge", headers=headers, json=ack).status_code == 409
    assert (
        client.get("/v1/sync/state", headers=headers).json()["agent_change_cursor"]
        == state["latest_change_id"]
    )


def test_history_scope_and_link_codes_require_browser_auth(app):
    application, client = app
    register(client)
    upload(client)
    read = client.post("/v1/tokens", json={"label": "read", "scopes": ["knowledge:read"]}).json()["token"]
    headers = {"Authorization": "Bearer " + read}
    assert client.post("/v1/native/link-code", headers=headers).status_code == 403
    assert client.post("/v1/sync/reconcile", headers=headers).status_code == 403
    register(client, "另一用户")
    assert client.get("/v1/changes").json()["items"] == []
    assert client.get("/v1/sync/state").json()["files"] == []
    # Native CORS is not permission to write unrelated knowledge endpoints with cookies.
    assert (
        client.post(
            "/v1/catalog/organize", headers={"Origin": application.state.settings.native_origin}
        ).status_code
        == 403
    )


def test_native_changes_update_index_preserve_manual_and_revoke_deleted(app, worker, monkeypatch, tmp_path):
    application, client = app
    user = register(client)
    tenant_id = tenant(application, user)
    payload = {"value": b"project alpha research"}
    files = {
        "F1": {
            "id": "F1",
            "type": 0,
            "name": "研究.txt",
            "path": "cloudreve://my/research.txt",
            "primary_entity": "v1",
            "size": len(payload["value"]),
            "updated_at": "v1",
        }
    }

    class Provider:
        def __init__(self, *_):
            pass

        def close(self):
            pass

        def inventory(self, *_):
            return {k: dict(v) for k, v in files.items()}

        @contextmanager
        def materialize(self, file, expected_sha=None, expected_size=None, **_):
            path = tmp_path / uuid4().hex
            path.write_bytes(payload["value"])
            sha = hashlib.sha256(payload["value"]).hexdigest()
            assert expected_sha is None or expected_sha == sha
            yield path, sha, len(payload["value"])
            path.unlink()

    monkeypatch.setattr(native, "NativeClient", Provider)
    with application.state.db.session(tenant_id) as session:
        session.add(NativeConnection(tenant_id=tenant_id, provider_user_id=uuid4().hex))

    def scan():
        with application.state.db.session(tenant_id) as session:
            native.enqueue(session, tenant_id)
        drain(worker)

    scan()
    source_id = client.get("/v1/sources").json()["items"][0]["id"]
    with application.state.db.session(tenant_id) as session:
        generation = session.get(Source, source_id).generation_id
        entry = session.scalar(select(CatalogEntry))
        entry.manual = {"title": "人工目录标题", "note": "保留我的注释"}
        entry_id = entry.id
    assert not client.get("/v1/sync/state").json()["needs_update"]
    files["F1"].update(name="研究改名.txt", path="cloudreve://my/renamed.txt", updated_at="renamed")
    scan()
    with application.state.db.session(tenant_id) as session:
        assert session.get(Source, source_id).generation_id == generation
        assert session.scalar(select(CatalogEntry)).id == entry_id
        assert session.scalar(select(CatalogEntry)).manual["title"] == "人工目录标题"
    payload["value"] = b"project omega updated evidence"
    files["F1"].update(primary_entity="v2", size=len(payload["value"]), updated_at="v2")
    scan()
    results = client.post("/v1/knowledge/search", json={"query": "omega"}).json()
    assert results["items"]
    assert not client.post("/v1/knowledge/search", json={"query": "alpha"}).json()["items"]
    event = next(e for e in client.get("/v1/changes").json()["items"] if e["action"] == "content_update")
    old = client.get(f"/v1/changes/{event['id']}/content?version=before").json()
    new = client.get(f"/v1/changes/{event['id']}/content?version=after").json()
    assert old["historical"] and "alpha" in old["items"][0]["text"]
    assert not new["historical"] and "omega" in new["items"][0]["text"]
    with application.state.db.session(tenant_id) as session:
        assert session.get(Source, source_id).generation_id != generation
        assert session.scalar(select(CatalogEntry)).manual["note"] == "保留我的注释"
    saved = files.pop("F1")
    scan()
    assert client.get(f"/v1/sources/{source_id}").status_code == 200
    scan()
    assert client.get(f"/v1/sources/{source_id}").status_code == 404
    assert not client.get(f"/v1/changes/{event['id']}/content?version=before").json()["content_available"]
    assert not client.post("/v1/knowledge/search", json={"query": "omega"}).json()["items"]
    files["F1"] = saved
    scan()
    assert client.get(f"/v1/sources/{source_id}").status_code == 200
    with application.state.db.session(tenant_id) as session:
        assert session.scalar(select(CatalogEntry)).manual["title"] == "人工目录标题"
    actions = [c["action"] for c in client.get("/v1/changes").json()["items"]]
    assert actions == ["create", "rename", "content_update", "delete", "restore"]
    assert (
        client.post("/v1/storage/plans", json={"source_ids": [source_id], "mode": "delete"}).status_code
        == 409
    )
    assert client.get("/v1/tasks").json()["items"]


def test_native_pairing_code_is_one_time_and_identity_scoped(app, monkeypatch):
    application, client = app
    first_user = register(client)
    first_tenant = tenant(application, first_user)
    provider_id = uuid4().hex
    monkeypatch.setattr(
        native, "verify_identity", lambda *_: {"id": provider_id, "email": "fixture@example.test"}
    )

    class LoginClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "user": {"id": provider_id},
                        "token": {
                            "access_token": "fixture",
                            "refresh_token": "separate-session",
                            "access_expires": (now() + timedelta(hours=1)).isoformat(),
                        },
                    },
                },
            )

    monkeypatch.setattr("akb.sync_api.httpx.Client", LoginClient)
    code = client.post("/v1/native/link-code").json()["code"]
    body = {"code": code, "access_token": "verified-fixture-token", "password": "fixture-password"}
    assert client.post("/v1/native/connect", json=body).status_code == 200
    assert client.post("/v1/native/connect", json=body).status_code == 403
    register(client, "另一知识库")
    body["code"] = client.post("/v1/native/link-code").json()["code"]
    assert client.post("/v1/native/connect", json=body).status_code == 409
    assert (
        list(application.state.settings.native_secrets_dir.glob("*.json"))[0].stat().st_mode & 0o777 == 0o600
    )
    with application.state.db.session(first_tenant) as session:
        session.scalar(select(NativeConnection)).enabled = False
        for job in session.scalars(select(Job).where(Job.kind == "native_sync")):
            job.status = "cancelled"


def test_native_download_rejects_redirect_origin_and_inflight_content_change(app, monkeypatch):
    settings = app[0].state.settings
    provider = native.NativeClient(settings, str(uuid4()))
    file = {
        "id": "test",
        "name": "test.txt",
        "path": "cloudreve://my/test.txt",
        "primary_entity": "v1",
        "size": 3,
    }
    monkeypatch.setattr(
        provider,
        "call",
        lambda method, path, **_: (
            dict(file) if path == "file/info" else {"urls": [{"url": "http://untrusted.invalid/private"}]}
        ),
    )
    with pytest.raises(OSError, match="只允许下载"):
        with provider.materialize(file, expected_size=3):
            pytest.fail("untrusted origin must not be fetched")
    reads = 0

    def call(method, path, **_):
        nonlocal reads
        if path == "file/info":
            reads += 1
            return {**file, "primary_entity": "v1" if reads == 1 else "v2"}
        return {"urls": [{"url": settings.native_cloudreve_url + "/fixture"}]}

    provider.client.close()
    provider.client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"old"))
    )
    monkeypatch.setattr(provider, "call", call)
    with pytest.raises(OSError, match="读取期间"):
        with provider.materialize(file, expected_size=3, expected_sha=hashlib.sha256(b"old").hexdigest()):
            pytest.fail("a changed version must never be published")
    provider.close()


def test_frontend_notices_retain_rapid_reverts_and_deduplicate(app, monkeypatch):
    application, client = app
    user = register(client)
    tenant_id = tenant(application, user)
    provider_id = uuid4().hex
    with application.state.db.session(tenant_id) as session:
        connection = NativeConnection(tenant_id=tenant_id, provider_user_id=provider_id)
        session.add(connection)
    monkeypatch.setattr(native, "verify_identity", lambda *_: {"id": provider_id})
    initial = client.get("/v1/sync/state").json()
    assert (
        client.post(
            "/v1/changes/acknowledge", json={"expected_hash": initial["current_hash"], "through_change_id": 0}
        ).status_code
        == 200
    )
    headers = {"X-Cloudreve-Token": "verified-fixture-token"}
    first = {
        "request_id": str(uuid4()),
        "operation": "rename",
        "targets": ["cloudreve://my/A.txt"],
        "new_name": "B.txt",
    }
    second = {**first, "request_id": str(uuid4()), "targets": ["cloudreve://my/B.txt"], "new_name": "A.txt"}
    assert client.post("/v1/native/notice", headers=headers, json=first).status_code == 202
    assert client.post("/v1/native/notice", headers=headers, json=first).json()["replayed"]
    assert (
        client.post(
            "/v1/native/notice", headers=headers, json={**first, "new_name": "tampered.txt"}
        ).status_code
        == 409
    )
    assert client.post("/v1/native/notice", headers=headers, json=second).status_code == 202
    state = client.get("/v1/sync/state").json()
    assert state["current_hash"] == initial["current_hash"] and state["agent_needs_review"]
    assert len(client.get("/v1/changes").json()["items"]) == 2
    assert not state["needs_update"]  # A report is never treated as a verified new content hash.
    with application.state.db.session(tenant_id) as session:
        session.scalar(select(NativeConnection)).enabled = False
        for job in session.scalars(select(Job)):
            job.status = "cancelled"
