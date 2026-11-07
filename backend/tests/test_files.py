from uuid import uuid4

from akb.api import create_app
from akb.config import Settings
from akb.models import User
from conftest import drain, register, upload
from fastapi.testclient import TestClient


def action(client, source, value, **kw):
    return client.post("/v1/files/actions", json={"ids": [source], "action": value, **kw})


def test_folders_upload_move_rename_and_cross_tenant(app, worker):
    application, client = app
    register(client)
    root = client.post("/v1/files/folders", json={"name": "教学材料"}).json()["id"]
    child = client.post("/v1/files/folders", json={"name": "课程", "parent_id": root}).json()["id"]
    assert (
        client.patch("/v1/files/folders/" + root, json={"name": "循环", "parent_id": child}).status_code
        == 409
    )
    assert client.post("/v1/files/folders", json={"name": "../材料"}).status_code == 422
    assert client.post("/v1/files/folders", json={"name": "教学材料"}).status_code == 409
    source = upload(client)["source"]["id"]
    assert action(client, source, "move", folder_id=child).status_code == 200
    assert client.get("/v1/files").json()["total"] == 0
    listing = client.get("/v1/files", params={"folder_id": child}).json()
    assert listing["items"][0]["id"] == source
    assert [x["name"] for x in listing["breadcrumbs"]] == ["教学材料", "课程"]
    assert client.patch("/v1/files/" + source, json={"name": "履历.txt"}).status_code == 200
    assert client.patch("/v1/files/" + source, json={"name": "履历.pdf"}).status_code == 422
    assert client.get("/v1/files", params={"query": "履历"}).json()["total"] == 1
    assert client.delete("/v1/files/folders/" + child).status_code == 409
    drain(worker)
    with TestClient(application) as other:
        register(other)
        assert other.get("/v1/files").json()["total"] == 0
        assert other.get("/v1/files", params={"folder_id": root}).status_code == 404
        assert other.patch("/v1/files/" + source, json={"name": "偷取.txt"}).status_code == 404
        assert action(other, source, "trash").status_code == 404


def test_trash_withdraws_evidence_restore_preserves_manual_then_purge(app, worker):
    _, client = app
    register(client)
    source = upload(client)["source"]["id"]
    drain(worker)
    catalog = client.get("/v1/catalog").json()
    entry = catalog["items"][0]
    assert (
        client.patch(
            "/v1/catalog/entries/" + entry["id"],
            json={"expected_revision": catalog["revision"], "summary": "我手写的摘要", "note": "保留批注"},
        ).status_code
        == 200
    )
    before = client.get("/v1/storage").json()["used_bytes"]
    evidence_id = client.get(f"/v1/sources/{source}/evidence").json()["items"][0]["id"]
    assert action(client, source, "trash").status_code == 200
    assert client.get(f"/v1/evidence/{evidence_id}").status_code == 404
    assert client.get(f"/v1/sources/{source}/original").status_code == 404
    assert not client.post("/v1/knowledge/search", json={"query": "陈舟"}).json()["items"]
    assert not client.get("/v1/catalog").json()["items"]
    drain(worker)
    assert client.get("/v1/storage").json()["used_bytes"] == before
    assert client.get("/v1/storage").json()["trash_bytes"] == before
    assert action(client, source, "restore").status_code == 200
    drain(worker)
    restored = client.get("/v1/catalog").json()["items"][0]
    assert restored["id"] == entry["id"]
    assert restored["summary"] == "我手写的摘要" and restored["note"] == "保留批注"
    assert client.get(f"/v1/evidence/{evidence_id}").status_code == 200
    assert action(client, source, "purge").status_code == 409
    assert action(client, source, "trash").status_code == 200
    assert action(client, source, "purge").status_code == 200
    drain(worker)
    assert client.get("/v1/storage").json()["used_bytes"] == 0
    assert client.get("/v1/files", params={"trash": True}).json()["total"] == 0


def test_upload_destination_and_agent_cannot_bypass_browser_approval(app, worker):
    application, client = app
    register(client)
    target = client.post("/v1/files/folders", json={"name": "研究"}).json()["id"]
    upload_id = client.post(
        "/v1/uploads", json={"filename": "材料.txt", "size": 3, "folder_id": target}
    ).json()["id"]
    assert client.delete("/v1/files/folders/" + target).status_code == 409
    assert client.put(f"/v1/uploads/{upload_id}/content?offset=0", content=b"abc").status_code == 200
    completed = client.post(f"/v1/uploads/{upload_id}/complete")
    assert completed.status_code == 202, completed.text
    source = completed.json()["source"]["id"]
    assert client.get("/v1/files", params={"folder_id": target}).json()["items"][0]["id"] == source
    token = client.post(
        "/v1/tokens", json={"label": "Agent", "scopes": ["knowledge:read", "knowledge:write"]}
    ).json()["token"]
    with TestClient(application) as agent:
        agent.headers["Authorization"] = "Bearer " + token
        assert agent.get("/v1/files").status_code == 200
        for act in ("trash", "purge", "restore", "move"):
            assert action(agent, source, act).status_code == 403
    drain(worker)


def test_real_cloudreve_roundtrip_isolated(app, worker):
    """Opt in only when the dedicated sidecar is available; no real user content is touched."""
    import pytest

    credentials = Settings().cloudreve_credentials
    if not credentials:
        pytest.skip("Dedicated Cloudreve is not configured")
    application, _ = app
    settings = application.state.settings.model_copy(update={"cloudreve_credentials": credentials})
    with TestClient(create_app(settings)) as client:
        identity = register(client, "Cloudreve 合成验收")
        source = upload(client, text="Cloudreve闭环测试 " + uuid4().hex)["source"]["id"]
        from akb.worker import Worker

        cloud_worker = Worker(settings)
        try:
            drain(cloud_worker)
            with application.state.db.session() as session:
                tenant = session.get(User, identity["id"]).tenant_id
            with application.state.db.session(tenant) as session:
                from akb.models import Source

                record = session.get(Source, source)
                assert record.object_key.startswith("cloudreve:")
                assert not (settings.object_root() / tenant / source / "original").exists()
            full = client.get(f"/v1/sources/{source}/original")
            assert full.status_code == 200 and "Cloudreve闭环测试" in full.text
            part = client.get(f"/v1/sources/{source}/original", headers={"Range": "bytes=0-4"})
            assert part.status_code == 206 and part.content == full.content[:5]
            assert client.get("/v1/catalog").json()["items"][0]["summary"]
            assert action(client, source, "trash").status_code == 200
            assert action(client, source, "restore").status_code == 200
            assert action(client, source, "trash").status_code == 200
            assert action(client, source, "purge").status_code == 200
            drain(cloud_worker)
            assert client.get("/v1/storage").json()["used_bytes"] == 0
        finally:
            cloud_worker.close()


def test_provider_failure_keeps_upload_reservation_and_allows_retry(app, monkeypatch):
    from akb import object_store

    application, _ = app
    settings = application.state.settings.model_copy(update={"cloudreve_credentials": application.state.settings.object_root() / "unused.json"})

    class Unavailable:
        def __init__(self, _settings):
            pass

        def put(self, key, path):
            raise OSError("synthetic provider outage")

        def close(self):
            pass

    monkeypatch.setattr(object_store, "Cloudreve", Unavailable)
    with TestClient(create_app(settings)) as client:
        register(client)
        upload_id = client.post("/v1/uploads", json={"filename": "重试.txt", "size": 3}).json()["id"]
        client.put(f"/v1/uploads/{upload_id}/content?offset=0", content=b"abc")
        failed = client.post(f"/v1/uploads/{upload_id}/complete")
        assert failed.status_code == 503
        quota = client.get("/v1/storage").json()
        assert quota["used_bytes"] == 0 and quota["reserved_bytes"] == 3
        assert client.get("/v1/files").json()["total"] == 0
        assert client.get(f"/v1/uploads/{upload_id}").json()["offset"] == 3
        # Recover against the local adapter: the same staging data and identifier survive.
        settings.cloudreve_credentials = None
        result = client.post(f"/v1/uploads/{upload_id}/complete")
        assert result.status_code == 202
        replay = client.post(f"/v1/uploads/{upload_id}/complete")
        assert replay.status_code == 202 and replay.json()["replayed"]
        quota = client.get("/v1/storage").json()
        assert quota["used_bytes"] == 3 and quota["reserved_bytes"] == 0
