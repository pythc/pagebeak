from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

from akb.models import CleanupPlan, Source, Upload, User, now
from conftest import drain, register, upload
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update


def quota(application, client, user_id, amount):
    # Only the operator DB role can bootstrap an administrator.
    engine = create_engine(application.state.settings.migration_database_url)
    with engine.begin() as connection:
        connection.execute(update(User).where(User.id == user_id).values(is_admin=True))
    engine.dispose()
    current = client.get("/v1/storage").json()["quota_bytes"]
    response = client.patch(
        f"/v1/admin/storage/users/{user_id}", json={"quota_bytes": amount, "expected_quota_bytes": current}
    )
    assert response.status_code == 200, response.text


def start(client, size, name="资料.txt"):
    return client.post("/v1/uploads", json={"filename": name, "size": size})


def test_reservation_chunks_finalize_replay_and_actual_deletion(app, worker):
    application, client = app
    user = register(client)
    quota(application, client, user["id"], 100)
    started = start(client, 90)
    assert started.status_code == 201
    upload_id = started.json()["id"]
    usage = client.get("/v1/storage").json()
    assert usage["near_full"] and usage["reserved_bytes"] == 90 and usage["used_bytes"] == 0
    assert start(client, 11).status_code == 413
    assert client.post(f"/v1/uploads/{upload_id}/complete").status_code == 409
    assert client.put(f"/v1/uploads/{upload_id}/content?offset=1", content=b"a").status_code == 409
    assert client.put(f"/v1/uploads/{upload_id}/content?offset=0", content=b"a" * 40).json()["offset"] == 40
    assert client.put(f"/v1/uploads/{upload_id}/content?offset=0", content=b"a").status_code == 409
    assert client.put(f"/v1/uploads/{upload_id}/content?offset=40", content=b"b" * 50).status_code == 200
    accepted = client.post(f"/v1/uploads/{upload_id}/complete")
    assert accepted.status_code == 202, accepted.text
    source_id = accepted.json()["source"]["id"]
    assert client.post(f"/v1/uploads/{upload_id}/complete").json()["replayed"]
    usage = client.get("/v1/storage").json()
    assert usage["used_bytes"] == 90 and usage["reserved_bytes"] == 0
    assert client.get(f"/v1/sources/{source_id}/original", headers={"Range": "bytes=0-3"}).content == b"aaaa"
    assert client.delete(f"/v1/sources/{source_id}").status_code == 202
    assert client.get("/v1/storage").json()["used_bytes"] == 90
    drain(worker)
    assert client.get("/v1/storage").json()["used_bytes"] == 0


def test_quota_cannot_be_over_reserved_concurrently_and_cancel_is_idempotent(app):
    application, client = app
    user = register(client)
    quota(application, client, user["id"], 100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(client, 70), range(2)))
    assert sorted(r.status_code for r in results) == [201, 413]
    upload_id = next(r.json()["id"] for r in results if r.status_code == 201)
    for _ in range(2):
        assert client.delete(f"/v1/uploads/{upload_id}").status_code == 200
    assert client.get("/v1/storage").json()["reserved_bytes"] == 0


def test_quota_applies_to_legacy_and_text_and_admin_is_browser_only(app):
    application, client = app
    user = register(client)
    assert client.get("/v1/admin/storage/users").status_code == 403
    quota(application, client, user["id"], 4)
    assert client.post("/v1/sources/text", json={"title": "x", "text": "12345"}).status_code == 413
    assert client.post("/v1/sources/upload", files={"file": ("x.txt", b"12345")}).status_code == 413
    token = client.post(
        "/v1/tokens", json={"label": "agent", "scopes": ["knowledge:read", "knowledge:write"]}
    ).json()["token"]
    with TestClient(application) as agent:
        agent.headers["Authorization"] = "Bearer " + token
        assert agent.get("/v1/admin/storage/users").status_code == 403
    assert start(client, 2 * 1024**3 + 1).status_code == 422
    assert (
        client.patch(
            f"/v1/admin/storage/users/{user['id']}", json={"quota_bytes": 100, "expected_quota_bytes": 0}
        ).status_code
        == 409
    )


def test_expired_uploads_release_space_and_recover_uncommitted_tail(app):
    application, client = app
    user = register(client)
    quota(application, client, user["id"], 100)
    upload_id = start(client, 10).json()["id"]
    assert client.put(f"/v1/uploads/{upload_id}/content?offset=0", content=b"12345").status_code == 200
    with application.state.db.session() as session:
        tenant_id = session.get(User, user["id"]).tenant_id
    path = application.state.settings.object_root() / tenant_id / "staging" / upload_id
    with path.open("ab") as stream:
        stream.write(b"uncommitted")
    assert client.put(f"/v1/uploads/{upload_id}/content?offset=5", content=b"67890").status_code == 200
    assert path.read_bytes() == b"1234567890"
    with application.state.db.session(tenant_id) as session:
        session.get(Upload, upload_id).expires_at = now() - timedelta(seconds=1)
    assert client.get("/v1/storage").json()["reserved_bytes"] == 0
    assert not path.exists()
    assert client.post(f"/v1/uploads/{upload_id}/complete").status_code == 409


def test_summary_cleanup_requires_exact_browser_authorization_and_retains_archive(app, worker):
    application, client = app
    register(client)
    source = upload(client, "姓名：测试教师\n工作单位：示例大学\n研究方向：文档管理")
    drain(worker)
    source_id = source["source"]["id"]
    token = client.post(
        "/v1/tokens", json={"label": "agent", "scopes": ["knowledge:read", "knowledge:write"]}
    ).json()["token"]
    with TestClient(application) as agent:
        agent.headers["Authorization"] = "Bearer " + token
        plan = agent.post("/v1/storage/plans", json={"source_ids": [source_id], "mode": "summarize"}).json()
        plan_id = plan["id"]
        assert "测试教师" in plan["summary"]
        assert agent.post(f"/v1/storage/plans/{plan_id}/execute").status_code == 403
        assert (
            agent.post(f"/v1/storage/plans/{plan_id}/approve", json={"acknowledge_loss": True}).status_code
            == 403
        )
        assert (
            client.post(f"/v1/storage/plans/{plan_id}/approve", json={"acknowledge_loss": False}).status_code
            == 409
        )
        assert (
            client.post(f"/v1/storage/plans/{plan_id}/approve", json={"acknowledge_loss": True}).status_code
            == 200
        )
        first = agent.post(f"/v1/storage/plans/{plan_id}/execute")
        assert first.status_code == 200, first.text
        assert agent.post(f"/v1/storage/plans/{plan_id}/execute").json()["result"] == first.json()["result"]
    assert client.get("/v1/storage").json()["used_bytes"] > 0
    drain(worker)
    assert client.get("/v1/storage").json()["used_bytes"] == 0
    archive = client.get("/v1/storage/archives").json()["items"][0]
    assert "测试教师" in archive["content"]
    assert client.get("/v1/catalog").json()["archives"][0]["id"] == archive["id"]
    assert "摘要归档" in client.get("/v1/catalog").json()["markdown"]
    assert client.get(f"/v1/sources/{source_id}/evidence").status_code == 404
    assert client.get("/v1/profile").json()["fields"]["name"]["status"] == "missing"
    assert client.delete(f"/v1/storage/archives/{archive['id']}").status_code == 204
    retained = client.get(f"/v1/storage/plans/{plan_id}").json()
    assert retained["summary"] == "" and retained["files"] == []


def test_plan_expiration_cancellation_version_drift_and_cross_tenant(app, worker):
    application, alice = app
    user = register(alice)
    source_id = upload(alice)["source"]["id"]
    drain(worker)
    with application.state.db.session() as session:
        tenant_id = session.get(User, user["id"]).tenant_id
    plan = alice.post("/v1/storage/plans", json={"source_ids": [source_id]}).json()
    with TestClient(application) as bob:
        register(bob)
        assert bob.get(f"/v1/storage/plans/{plan['id']}").status_code == 404
        assert bob.post("/v1/storage/plans", json={"source_ids": [source_id]}).status_code == 404
        upload_id = start(alice, 10).json()["id"]
        assert bob.get(f"/v1/uploads/{upload_id}").status_code == 404
    with application.state.db.session(tenant_id) as session:
        session.get(CleanupPlan, plan["id"]).expires_at = now() - timedelta(seconds=1)
    assert (
        alice.post(f"/v1/storage/plans/{plan['id']}/approve", json={"acknowledge_loss": True}).status_code
        == 409
    )
    fresh = alice.post("/v1/storage/plans", json={"source_ids": [source_id]}).json()
    assert (
        alice.post(f"/v1/storage/plans/{fresh['id']}/approve", json={"acknowledge_loss": True}).status_code
        == 200
    )
    with application.state.db.session(tenant_id) as session:
        session.get(Source, source_id).revision += 1
    assert alice.post(f"/v1/storage/plans/{fresh['id']}/execute").status_code == 409
    assert alice.post(f"/v1/storage/plans/{fresh['id']}/cancel").status_code == 200
    assert alice.post(f"/v1/storage/plans/{fresh['id']}/execute").status_code == 403
    assert alice.get(f"/v1/sources/{source_id}").status_code == 200


def test_quota_cannot_shrink_below_used_and_pending_uploads(app):
    application, client = app
    user = register(client)
    quota(application, client, user["id"], 100)
    start(client, 70)
    response = client.patch(
        f"/v1/admin/storage/users/{user['id']}", json={"quota_bytes": 60, "expected_quota_bytes": 100}
    )
    assert response.status_code == 409
    assert client.get("/v1/storage").json()["quota_bytes"] == 100


def test_chunked_overflow_and_generated_summary_spans_document(app, worker):
    _, client = app
    register(client)
    source = upload(client, "\n".join(f"项目第 {i} 节：独立内容 {uuid4().hex}" for i in range(40)))
    drain(worker)
    item = next(
        i for i in client.get("/v1/catalog").json()["items"] if i["source_id"] == source["source"]["id"]
    )
    assert item["generated"]["blocks_scanned"] == 40
    assert "项目第 39 节" in item["summary"]
    assert item["generated"]["summary_kind"] == "document_extracts"
    assert (
        client.put(f"/v1/uploads/{uuid4()}/content?offset=0", content=iter([b"x" * 1024**2] * 9)).status_code
        == 413
    )


def test_cleanup_preserves_manual_content_and_rejects_changes_after_approval(app, worker):
    _, client = app
    register(client)
    source_id = upload(client)["source"]["id"]
    drain(worker)
    catalog = client.get("/v1/catalog").json()
    entry = next(i for i in catalog["items"] if i["source_id"] == source_id)
    edited = client.patch(
        f"/v1/catalog/entries/{entry['id']}",
        json={"expected_revision": catalog["revision"], "summary": "用户整理的摘要", "note": "重要保留说明"},
    )
    assert edited.status_code == 200
    plan = client.post("/v1/storage/plans", json={"source_ids": [source_id], "mode": "summarize"}).json()
    assert "用户整理的摘要" in plan["summary"] and "重要保留说明" in plan["summary"]
    assert (
        client.post(f"/v1/storage/plans/{plan['id']}/approve", json={"acknowledge_loss": True}).status_code
        == 200
    )
    current = client.get("/v1/catalog").json()
    assert (
        client.patch(
            f"/v1/catalog/entries/{entry['id']}",
            json={"expected_revision": current["revision"], "note": "批准后又修改"},
        ).status_code
        == 200
    )
    assert client.post(f"/v1/storage/plans/{plan['id']}/execute").status_code == 409
    assert client.get(f"/v1/sources/{source_id}").status_code == 200


def test_agent_summary_citations_checked_and_summary_survives_original_cleanup(app, worker):
    _, client = app
    register(client)
    source_id = upload(client, "项目：知识库升级\n目标：统一索引\n实施年份：2026")["source"]["id"]
    other = upload(client, "另一个来源的内容")["source"]["id"]
    drain(worker)
    evidence = client.get(f"/v1/sources/{source_id}/evidence").json()["items"]
    foreign = client.get(f"/v1/sources/{other}/evidence").json()["items"][0]
    body = {
        "source_ids": [source_id],
        "mode": "summarize",
        "summaries": [
            {
                "source_id": source_id,
                "content": "2026 年实施知识库升级，目标是统一索引。",
                "citations": [{"evidence_id": e["id"], "quote": e["text"]} for e in evidence],
            }
        ],
    }
    citation = body["summaries"][0]["citations"][0]
    correct = dict(citation)
    citation["quote"] = "未出现在原文中的虚构引文"
    assert client.post("/v1/storage/plans", json=body).status_code == 422
    citation.update(evidence_id=foreign["id"], quote=foreign["text"])
    assert client.post("/v1/storage/plans", json=body).status_code == 422
    citation.update(correct)
    accepted = client.post("/v1/storage/plans", json=body)
    assert accepted.status_code == 201, accepted.text
    plan = accepted.json()
    assert body["summaries"][0]["content"] in plan["summary"]
    assert plan["files"][0]["agent_summary"]["kind"] == "agent_generated_unverified"
    assert (
        client.post(f"/v1/storage/plans/{plan['id']}/approve", json={"acknowledge_loss": True}).status_code
        == 200
    )
    assert client.post(f"/v1/storage/plans/{plan['id']}/execute").status_code == 200
    drain(worker)
    archive = client.get("/v1/storage/archives", params={"query": "2026 年实施"}).json()["items"][0]
    assert "2026 年实施知识库升级" in archive["content"]
    assert archive["manifest"][0]["agent_summary"]["citations"][0]["quote"] == correct["quote"]
    assert client.get(f"/v1/sources/{source_id}/evidence").status_code == 404
