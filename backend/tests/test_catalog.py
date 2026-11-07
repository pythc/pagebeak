from datetime import timedelta
from uuid import uuid4

from akb.models import Catalog, CatalogEntry, Job, Source, User, now
from akb.organization import enqueue
from conftest import drain, register
from fastapi.testclient import TestClient
from sqlalchemy import select


def imported(client, title, text, scope="reference"):
    response = client.post(
        "/v1/sources/text",
        json={"title": title, "text": text, "subject_scope": scope},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 202, response.text
    return response.json()["source"]["id"]


def catalog(client, **params):
    result = client.get("/v1/catalog", params=params)
    assert result.status_code == 200, result.text
    return result.json()


def patch(client, path="/v1/catalog", **changes):
    result = client.patch(path, json={"expected_revision": catalog(client)["revision"], **changes})
    assert result.status_code == 200, result.text
    return result.json()


def test_backfill_existing_library_and_paginate_same_directory(app, worker):
    _, client = app
    register(client)
    identity = catalog(client)["id"]
    patch(client, enabled=False)
    imported(client, "个人履历.txt", "姓名：陈舟\n职称：讲师", "self")
    imported(client, "项目说明.txt", "项目：知序\n内容：资料整理")
    imported(client, "收藏论文.txt", "论文：知识检索\n作者：王样本")
    drain(worker)
    assert catalog(client)["pending"] == 3
    assert all(i["id"] is None for i in catalog(client)["items"])
    job = client.post("/v1/catalog/organize").json()["job_id"]
    drain(worker)
    data = catalog(client)
    assert data["id"] == identity
    assert data["pending"] == 0 and data["total"] == 3
    assert len(data["last_result"]["added"]) == 3
    assert client.get("/v1/jobs/" + job).json()["result"]["sources_read"] == 3
    assert {s["name"] for s in data["sections"]} == {"个人与工作", "项目资料", "论文与参考文献"}
    assert "不代表本人成果" in data["markdown"]
    first, second = catalog(client, limit=2), catalog(client, offset=2, limit=2)
    assert first["has_more"] and not second["has_more"]
    assert first["id"] == second["id"] == identity
    assert not ({i["id"] for i in first["items"]} & {i["id"] for i in second["items"]})


def test_incremental_update_retains_every_manual_field_and_stable_ids(app, worker):
    _, client = app
    register(client)
    first = imported(client, "项目甲.txt", "项目甲：首次材料")
    second = imported(client, "项目乙.txt", "项目乙：保持不变")
    drain(worker)
    before = catalog(client)
    original = next(i for i in before["items"] if i["source_id"] == first)
    untouched = next(i for i in before["items"] if i["source_id"] == second)
    patch(client, title="我的长期目录", note="人工目录说明")
    custom = {"title": "人工标题", "category": "教学专项", "summary": "人工摘要", "note": "请保留我的备注"}
    patch(client, "/v1/catalog/entries/" + original["id"], **custom)
    added = imported(client, "新增论文.txt", "论文：新增研究\n作者：另一位作者")
    drain(worker)
    after = catalog(client)
    current = next(i for i in after["items"] if i["source_id"] == first)
    assert after["id"] == before["id"]
    assert after["title"] == "我的长期目录" and after["note"] == "人工目录说明"
    assert current["id"] == original["id"]
    assert all(current[k] == v for k, v in custom.items())
    assert current["generated_at"] == original["generated_at"]
    assert (
        next(i for i in after["items"] if i["source_id"] == second)["generated_at"]
        == untouched["generated_at"]
    )
    assert after["last_result"]["added"] == [added]
    assert after["last_result"]["unchanged"] == 2
    assert after["last_result"]["sources_read"] == 1
    assert len({i["source_id"] for i in after["items"]}) == after["total"] == 3
    # Reconciliation is idempotent and never replaces the user's work.
    client.post("/v1/catalog/organize")
    drain(worker)
    unchanged = catalog(client)
    assert unchanged["last_result"]["sources_read"] == 0
    assert unchanged["revision"] == after["revision"]
    assert "人工摘要" in unchanged["markdown"] and "用户摘要" in unchanged["markdown"]
    patch(client, "/v1/catalog/entries/" + original["id"], title=None, category=None, summary=None)
    restored = next(i for i in catalog(client)["items"] if i["source_id"] == first)
    assert restored["title"] == original["title"] and restored["note"] == custom["note"]
    assert restored["manual_fields"] == ["note"]


def test_manual_edit_optimistic_conflict_does_not_lose_either_edit(app, worker):
    _, client = app
    register(client)
    imported(client, "项目.txt", "项目内容")
    drain(worker)
    before = catalog(client)
    patch(client, note="另一窗口的更改")
    response = client.patch(
        "/v1/catalog",
        json={
            "expected_revision": before["revision"],
            "note": "旧窗口的更改",
        },
    )
    assert response.status_code == 409
    assert catalog(client)["note"] == "另一窗口的更改"
    assert (
        client.patch(
            "/v1/catalog",
            json={
                "expected_revision": catalog(client)["revision"],
                "title": "   ",
            },
        ).status_code
        == 422
    )


def test_source_change_suppresses_stale_excerpts_then_updates_only_that_entry(app, worker):
    application, client = app
    user = register(client)
    source_id = imported(client, "旧项目.txt", "项目旧内容")
    drain(worker)
    before = catalog(client)
    patch(client, "/v1/catalog/entries/" + before["items"][0]["id"], title="人工固定标题")
    with application.state.db.session() as session:
        tenant_id = session.get(User, user["id"]).tenant_id
    with application.state.db.session(tenant_id) as session:
        source = session.get(Source, source_id)
        source.title, source.revision = "新项目.txt", source.revision + 1
        enqueue(session, tenant_id, force=True)
    pending = catalog(client)["items"][0]
    assert not pending["current"] and not pending["generated"]["excerpts"]
    assert pending["title"] == "人工固定标题"
    drain(worker)
    after = catalog(client)
    assert after["items"][0]["generated"]["title"] == "新项目.txt"
    assert after["items"][0]["title"] == "人工固定标题"
    assert after["last_result"]["updated"] == [source_id]


def test_revocation_hides_directory_entry_before_cleanup_and_removes_manual_data(app, worker):
    application, client = app
    user = register(client)
    source_id = imported(client, "项目.txt", "需要删除的项目内容")
    drain(worker)
    entry = catalog(client)["items"][0]
    patch(client, "/v1/catalog/entries/" + entry["id"], summary="私有人工摘要", note="私有备注")
    patch(client, enabled=False)
    assert client.delete("/v1/sources/" + source_id).status_code == 202
    assert catalog(client)["total"] == 0
    assert "私有" not in catalog(client)["markdown"]
    assert (
        client.patch(
            "/v1/catalog/entries/" + entry["id"],
            json={
                "expected_revision": catalog(client)["revision"],
                "note": "继续编辑",
            },
        ).status_code
        == 404
    )
    drain(worker)
    with application.state.db.session() as session:
        tenant_id = session.get(User, user["id"]).tenant_id
    with application.state.db.session(tenant_id) as session:
        assert session.get(CatalogEntry, entry["id"]) is None


def test_directory_isolation_scopes_and_no_profile_scope_bypass(app, worker):
    application, alice = app
    register(alice)
    imported(alice, "本人履历.txt", "姓名：陈舟\n邮箱：chen@example.test", "self")
    drain(worker)
    data = catalog(alice)
    token = alice.post("/v1/tokens", json={"label": "目录只读", "scopes": ["knowledge:read"]}).json()["token"]
    with TestClient(application) as agent:
        agent.headers["Authorization"] = "Bearer " + token
        shared = catalog(agent)
        assert shared["id"] == data["id"] and shared["items"] == data["items"]
        assert shared["profile"] is None
        assert agent.post("/v1/catalog/organize").status_code == 403
        assert agent.patch("/v1/catalog", json={"expected_revision": 1, "note": "篡改"}).status_code == 403
    with TestClient(application) as bob:
        register(bob, "另一个用户")
        assert catalog(bob)["items"] == []
        assert catalog(bob)["id"] != data["id"]
        assert (
            bob.patch(
                "/v1/catalog/entries/" + data["items"][0]["id"],
                json={
                    "expected_revision": catalog(bob)["revision"],
                    "note": "越权",
                },
            ).status_code
            == 404
        )
    with application.state.db.session() as session:
        assert list(session.scalars(select(Catalog))) == []
        assert list(session.scalars(select(CatalogEntry))) == []


def test_catalog_tasks_coalesce_and_do_not_invalidate_form_drafts(app, worker):
    _, client = app
    register(client)
    imported(client, "本人履历.txt", "姓名：陈舟", "self")
    drain(worker)
    draft = client.post(
        "/v1/forms/drafts",
        json={
            "title": "表单",
            "fields": [{"key": "name", "label": "姓名", "fact_key": "name"}],
        },
    ).json()
    first = client.post("/v1/catalog/organize").json()
    assert client.post("/v1/catalog/organize").json()["job_id"] == first["job_id"]
    patch(client, note="仅更改目录，不更改个人事实")
    drain(worker)
    assert not client.get("/v1/forms/drafts/" + draft["id"]).json()["stale"]


def test_reenabling_automatic_organization_catches_changes_made_while_paused(app, worker):
    _, client = app
    register(client)
    patch(client, enabled=False)
    imported(client, "项目.txt", "项目新增内容")
    drain(worker)
    assert catalog(client)["pending"] == 1
    patch(client, enabled=True)
    drain(worker)
    assert catalog(client)["pending"] == 0 and catalog(client)["total"] == 1


def test_worker_exhaustion_refreshes_a_previously_organized_pending_document(app, worker):
    _, client = app
    register(client)
    imported(client, "项目.txt", "尚未完成解析")
    ingestion = worker.claim()
    directory = worker.claim()
    worker.process(directory)
    assert catalog(client)["pending"] == 0
    assert catalog(client)["items"][0]["status"] == "queued"
    with worker.db.session(ingestion["tenant_id"]) as session:
        job = session.get(Job, ingestion["job_id"])
        job.attempts, job.lease_until = 5, now() - timedelta(seconds=1)
    # The dispatcher inserts a durable refresh while finalizing the exhausted source.
    ticket = worker.claim()
    if ticket:
        worker.process(ticket)
    drain(worker)
    after = catalog(client)
    assert after["pending"] == 0 and after["items"][0]["status"] == "failed"


def test_expired_catalog_worker_cannot_publish_over_a_new_lease(app, worker):
    _, client = app
    register(client)
    client.post("/v1/catalog/organize")
    old = worker.claim()
    with worker.db.session(old["tenant_id"]) as session:
        session.get(Job, old["job_id"]).lease_until = now() - timedelta(seconds=1)
    fresh = worker.claim()
    assert fresh["fencing_token"] > old["fencing_token"]
    worker.process(old)
    assert catalog(client)["organized_at"] is None
    worker.process(fresh)
    assert catalog(client)["organized_at"] is not None
