"""Real Tencent services integration. Run with AKB_TEST_TENCENT=1 (ports 8420/8421)."""
import os
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from akb.api import create_app
from akb.models import MemoryObject, Source, now
from akb.worker import Worker
from conftest import drain, register, upload
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(os.getenv("AKB_TEST_TENCENT") != "1", reason="requires real Tencent sidecars")


@pytest.fixture
def memory_app(test_settings, tmp_path):
    old = Worker(test_settings)
    drain(old)
    old.close()
    settings = test_settings.model_copy(update={"knowledge_engine": "tencent", "storage_dir": tmp_path})
    application = create_app(settings)
    worker = Worker(settings)
    with TestClient(application) as browser:
        register(browser)
        token = browser.post("/v1/tokens", json={"label": "Tencent integration", "scopes": [
            "knowledge:read", "knowledge:write", "profile:read"]}).json()["token"]
        tenant = browser.get("/v1/context").json()["knowledge_base_id"]
        with TestClient(application) as agent:
            agent.headers.update({"Authorization": "Bearer " + token, "X-AKB-Knowledge-Base": tenant})
            yield application, browser, agent, worker, tenant
    worker.close()


def item(kind="preference", **changes):
    return {"request_id": str(uuid4()), "kind": kind, "key": "writing", "title": "写作习惯",
            "content": "用户明确希望使用简洁中文，先列数据出处。", "expected_revision": 0, **changes}


def publish(client, body):
    r = client.post("/v2/library/objects", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_real_memory_cross_session_isolation_long_body_and_legacy_retired(memory_app):
    app, browser, agent, _, tenant = memory_app
    content = "简洁中文，先列数据出处。" * 600
    body = item(content=content)
    saved = publish(agent, body)
    with TestClient(app) as new_session:
        new_session.headers.update(dict(agent.headers))
        assert new_session.get(f"/v2/library/objects/{saved['id']}").json()["content"] == content
        assert new_session.post("/v2/library/search", json={"query": "中文"}).json()["items"][0]["id"] == saved["id"]
    with TestClient(app) as other:
        register(other)
        assert other.get(f"/v2/library/objects/{saved['id']}").status_code == 404
        assert other.post("/v2/library/search", json={"query": "中文"}).json()["items"] == []
        assert publish(other, body)["id"] != saved["id"]
    assert browser.get("/v1/context").json()["knowledge_engine"] == "tencent"
    assert browser.get("/v1/profile").status_code == 404
    assert browser.get("/", follow_redirects=False).headers["location"].endswith("/knowledge")
    assert browser.get("/index.html").status_code == 404
    connect_prompt = browser.post("/v1/onboarding/connection").json()["prompt"]
    assert "腾讯知识页" in connect_prompt and "提出候选" not in connect_prompt
    assert browser.post("/v1/knowledge/facts/accept", json={}).status_code in {404, 405}
    for port, path in [(8420, "/v3/conversation/search"), (8421, "/v3/wiki/list")]:
        assert httpx.post(f"http://127.0.0.1:{port}{path}", json={}, trust_env=False).status_code == 401


def test_real_wiki_sources_manual_edit_invalidation_and_deletion(memory_app):
    app, browser, agent, worker, tenant = memory_app
    source = upload(browser, "周雨桐在2025年负责青禾项目；批准经费为12万元，到账8万元。")
    drain(worker)
    catalog = agent.get("/v2/library").json()
    assert catalog["pending"] == 1
    versions = {s["id"]: s["version"] for s in catalog["sources"]}
    body = item("page", key="qinghe", title="青禾项目", content="批准12万元，到账8万元，二者不能混为实支。", source_versions=versions)
    saved = publish(agent, body)
    assert agent.get(f"/v2/library/objects/{saved['id']}").json()["content"] == body["content"]
    assert agent.post("/v2/library/search", json={"query": "青禾"}).json()["items"][0]["id"] == saved["id"]
    assert agent.get("/v2/library").json()["pending"] == 0
    manual = {**body, "request_id": str(uuid4()), "expected_revision": 1, "content": "人工提醒：请区分批准额和到账额。"}
    assert publish(browser, manual)["protected"]
    rewrite = {**manual, "request_id": str(uuid4()), "expected_revision": 2, "content": "覆盖人工修改"}
    assert agent.post("/v2/library/objects", json=rewrite).status_code == 409
    with app.state.db.session(tenant) as db:
        src = db.get(Source, source["source"]["id"])
        src.revision += 1
    stale = agent.get(f"/v2/library/objects/{saved['id']}").json()
    assert not stale["current"] and stale["content"] is None and stale["protected"]
    assert stale["historical_content"] == manual["content"]
    assert agent.post("/v2/library/search", json={"query": "青禾"}).json()["items"] == []
    assert agent.post("/v2/library/objects", json=item("page", source_versions=versions)).status_code == 409
    assert browser.delete(f"/v1/sources/{source['source']['id']}").status_code == 202
    drain(worker)
    assert agent.get(f"/v2/library/objects/{saved['id']}").status_code == 404


def test_real_forget_replay_expiry_and_failure_closed(memory_app, monkeypatch):
    app, browser, agent, worker, tenant = memory_app
    body = item()
    saved = publish(agent, body)
    assert publish(agent, body)["replayed"]
    forgotten = agent.post(f"/v2/library/objects/{saved['id']}/forget", json={"expected_revision": 1})
    assert forgotten.status_code == 200, forgotten.text
    assert forgotten.json()["deleted"] and not forgotten.json()["purge_pending"]
    assert publish(agent, body)["deleted"]
    assert agent.get(f"/v2/library/objects/{saved['id']}").status_code == 404
    assert agent.post("/v2/library/objects", json=item(key="another-key")).status_code == 409
    active = publish(agent, item(key="expiry", content="短期准备青禾申报", expires_at=(now() + timedelta(days=1)).isoformat()))
    with app.state.db.session(tenant) as db:
        db.get(MemoryObject, active["id"]).expires_at = now() - timedelta(seconds=1)
    assert agent.get(f"/v2/library/objects/{active['id']}").json()["content"] is None
    assert agent.post("/v2/library/search", json={"query": "申报"}).json()["items"] == []
    monkeypatch.setattr(app.state.settings, "memory_core_url", "http://127.0.0.1:1")
    r = agent.post("/v2/library/objects", json=item(key="offline", content="不可写入的偏好"))
    assert r.status_code == 503
    assert "offline" not in [o["key"] for o in agent.get("/v2/library").json()["items"]]
