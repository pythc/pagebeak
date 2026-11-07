"""Local-to-cloud ownership, lifecycle and provenance against real Tencent sidecars."""

import os
from uuid import uuid4

import pytest
from akb.models import Source
from conftest import drain, register, upload
from fastapi.testclient import TestClient
from test_tencent_memory import item, publish
from test_tencent_memory import memory_app as memory_app  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("AKB_TEST_TENCENT") != "1", reason="requires real Tencent sidecars")


def test_catalog_cas_routing_pagination_privacy_and_rls(memory_app):
    app, browser, agent, _, _ = memory_app
    device = str(uuid4())
    entry = {
        "id": str(uuid4()),
        "root_label": "教学",
        "root_id": "a" * 12,
        "path": "课程/年度总结.txt",
        "sha256": "b" * 64,
        "size": 100,
        "status": "indexed",
    }
    body = {"device_id": device, "snapshot_id": str(uuid4()), "expected_revision": 0, "items": [entry]}
    r = agent.post("/v2/local/catalog", json=body)
    assert r.status_code == 200 and r.json()["revision"] == 1, r.text
    assert agent.post("/v2/local/catalog", json=body).json()["replayed"]
    conflict = {**body, "snapshot_id": str(uuid4())}
    assert agent.post("/v2/local/catalog", json=conflict).status_code == 409
    listing = browser.get("/v2/local/catalog").json()
    assert listing["items"][0]["path"] == entry["path"]
    assert browser.get("/v2/library").json()["local_catalog"]["total"] == 1
    assert agent.get(f"/v2/local/catalog/{device}?offset=1").json()["items"] == []
    for bad in ({"path": "/Users/private/file.txt"}, {"path": "../other/file.txt"}, {"text": "不应同步正文"}):
        assert (
            agent.post(
                "/v2/local/catalog",
                json={
                    **body,
                    "expected_revision": 1,
                    "snapshot_id": str(uuid4()),
                    "items": [{**entry, **bad}],
                },
            ).status_code
            == 422
        )
    with TestClient(app) as other:
        register(other)
        assert other.get("/v2/local/catalog").json()["items"] == []
        assert other.get(f"/v2/local/catalog/{device}").json()["revision"] == 0
    assert (
        agent.post(
            "/v2/local/catalog",
            json={**body, "expected_revision": 1, "snapshot_id": str(uuid4()), "items": []},
        ).status_code
        == 200
    )
    assert agent.get("/v2/local/catalog").json()["total"] == 0


def test_changed_import_retires_old_evidence_preserves_manual_and_snapshot(memory_app):
    app, browser, agent, worker, tenant = memory_app
    incoming = upload(browser, "陈婉清现聘讲师。")
    drain(worker)
    src = incoming["source"]
    sid = src["id"]
    version = agent.get("/v2/library").json()["sources"][0]["version"]
    evidence = agent.get(f"/v1/sources/{sid}/evidence").json()["items"][0]
    anchor = {"evidence_id": evidence["id"], "source_version": version, "quote": "陈婉清现聘讲师。"}
    body = item(
        "page",
        key="employment",
        title="任职",
        content="陈婉清现聘讲师。",
        source_versions={sid: version},
        citations=[{"statement": "陈婉清现聘讲师。", "evidence": anchor}],
    )
    saved = publish(agent, body)
    publish(
        browser,
        {
            **body,
            "request_id": str(uuid4()),
            "expected_revision": 1,
            "content": "人工备注：仅用于任职核对。",
            "citations": [],
        },
    )
    observation = {
        "device_id": str(uuid4()),
        "items": [
            {
                "document_id": str(uuid4()),
                "source_id": sid,
                "sha256": src["sha256"],
                "current_hash": src["sha256"],
                "status": "indexed",
            }
        ],
    }
    assert agent.post("/v2/local/observe", json=observation).status_code == 200
    # Offline or local deletion does not delete the already-uploaded original.
    observation["items"][0]["status"] = "missing"
    assert agent.post("/v2/local/observe", json=observation).json()["stale_source_ids"] == []
    assert agent.get(f"/v2/library/objects/{saved['id']}").json()["current"]
    # A content change invalidates the old version even before another upload exists.
    observation["items"][0].update(current_hash="a" * 64, status="indexed")
    r = agent.post("/v2/local/observe", json=observation)
    assert r.status_code == 200 and r.json()["stale_source_ids"] == [sid], r.text
    stale = agent.get(f"/v2/library/objects/{saved['id']}").json()
    assert (
        not stale["current"]
        and stale["protected"]
        and stale["historical_content"] == "人工备注：仅用于任职核对。"
    )
    assert sid not in [s["id"] for s in agent.get("/v2/library").json()["sources"]]
    assert agent.post("/v2/library/evidence/resolve", json=anchor).status_code == 409
    assert not agent.post("/v2/local/match", json={"sha256": src["sha256"], "subject_scope": "self"}).json()[
        "source"
    ]
    with app.state.db.session(tenant) as session:
        assert session.get(Source, sid).object_key
    with TestClient(app) as other:
        register(other)
        assert other.post("/v2/local/observe", json=observation).status_code == 404
