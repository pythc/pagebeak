from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from akb.models import Evidence, Job, Source, User, now
from akb.services import object_path
from akb.worker import Worker
from conftest import drain, register, upload
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError


def test_real_ingestion_profile_and_form(app, worker):
    application, client = app
    register(client)
    imported = upload(client)
    assert client.get(f"/v1/jobs/{imported['job_id']}").json()["status"] == "queued"
    drain(worker)
    source = client.get(f"/v1/sources/{imported['source']['id']}").json()
    assert source["status"] == "ready"
    results = client.post("/v1/knowledge/search", json={"query": "示例大学"}).json()
    assert len(results["items"]) == 1
    evidence = client.get(f"/v1/evidence/{results['items'][0]['id']}").json()
    assert evidence["locator"]["line"] == 2
    profile = client.get("/v1/profile").json()
    assert profile["fields"]["name"]["value"] == "陈舟"
    assert profile["fields"]["job_title"]["status"] == "missing"
    candidate = next(c for c in profile["candidates"] if c["field"] == "job_title")
    response = client.post(
        f"/v1/claims/{candidate['id']}/decisions",
        json={"action": "accept", "expected_revision": candidate["revision"]},
    )
    assert response.status_code == 200
    draft = client.post(
        "/v1/forms/drafts",
        json={
            "title": "报名表",
            "fields": [
                {"key": "name", "label": "姓名", "fact_key": "name"},
                {"key": "title", "label": "职称", "fact_key": "job_title"},
                {"key": "email", "label": "邮箱", "fact_key": "email"},
                {"key": "unknown", "label": "论文总数", "fact_key": "publication_count"},
            ],
        },
    ).json()
    assert [a["status"] for a in draft["answers"]] == ["ready", "ready", "blocked", "missing"]
    assert draft["answers"][1]["value"] == "讲师"


def test_reference_document_is_not_personal_profile(app, worker):
    _, client = app
    register(client)
    upload(client, scope="reference")
    drain(worker)
    profile = client.get("/v1/profile").json()
    assert not profile["candidates"]
    assert all(f["status"] == "missing" for f in profile["fields"].values())


def test_generated_page_keeps_draft_valid_but_fact_confirmation_invalidates_it(app, worker):
    _, client = app
    register(client)
    imported = upload(client)
    drain(worker)
    profile = client.get("/v1/profile").json()
    candidate = next(c for c in profile["candidates"] if c["field"] == "organization")
    evidence = client.get(f"/v1/sources/{imported['source']['id']}/evidence").json()["items"][0]
    draft = client.post(
        "/v1/forms/drafts",
        json={"title": "登记表", "fields": [{"key": "name", "label": "姓名", "fact_key": "name"}]},
    ).json()

    saved = client.post(
        "/v1/pages", json={"title": "资料目录", "content": "陈舟", "evidence_ids": [evidence["id"]]}
    )
    assert saved.status_code == 201
    current = client.get(f"/v1/forms/drafts/{draft['id']}").json()
    assert not current["stale"]
    assert current["answers"] == draft["answers"]

    confirmed = client.post(
        f"/v1/claims/{candidate['id']}/decisions",
        json={"action": "accept", "expected_revision": candidate["revision"]},
    )
    assert confirmed.status_code == 200
    changed = client.get(f"/v1/forms/drafts/{draft['id']}").json()
    assert changed["stale"] and changed["answers"] == []


def test_idempotency_conflict_and_replay(app, worker):
    _, client = app
    register(client)
    key = uuid4().hex
    first, second = upload(client, key=key), upload(client, key=key)
    assert first["source"]["id"] == second["source"]["id"]
    assert first["job_id"] == second["job_id"] and second["replayed"]
    bad = client.post(
        "/v1/sources/upload",
        files={"file": ("简历.txt", b"different", "text/plain")},
        data={"subject_scope": "self"},
        headers={"Idempotency-Key": key},
    )
    assert bad.status_code == 409
    assert len(client.get("/v1/sources").json()["items"]) == 1


def test_cross_tenant_resources_are_not_visible(app, worker):
    application, alice = app
    register(alice)
    source = upload(alice)
    drain(worker)
    evidence = alice.post("/v1/knowledge/search", json={"query": "陈舟"}).json()["items"][0]
    claim = alice.get("/v1/profile").json()["candidates"][0]
    with TestClient(application) as bob:
        register(bob, "另一个用户")
        for path in [
            f"/sources/{source['source']['id']}",
            f"/sources/{source['source']['id']}/original",
            f"/evidence/{evidence['id']}",
            f"/jobs/{source['job_id']}",
        ]:
            assert bob.get("/v1" + path).status_code == 404
        assert bob.post("/v1/knowledge/search", json={"query": "陈舟"}).json()["items"] == []
        assert (
            bob.post(
                f"/v1/claims/{claim['id']}/decisions", json={"action": "accept", "expected_revision": 1}
            ).status_code
            == 404
        )
        assert bob.delete(f"/v1/sources/{source['source']['id']}").status_code == 404
        assert (
            bob.post(
                "/v1/pages", json={"title": "越权页", "content": "test", "evidence_ids": [evidence["id"]]}
            ).status_code
            == 404
        )


def test_database_rls_no_context_and_connection_reuse(app, worker):
    application, client = app
    user = register(client)
    source = upload(client)
    db = application.state.db
    with db.session() as session:
        row = session.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
        ).one()
        assert row == (False, False)
        assert session.scalar(select(Source).where(Source.id == source["source"]["id"])) is None
        with pytest.raises(DBAPIError):
            session.execute(text("SELECT * FROM public.akb_claim_job('attacker', 5)"))
        session.rollback()
    with db.session() as session:
        tenant_id = session.get(User, user["id"]).tenant_id
    with db.session(tenant_id) as session:
        assert session.get(Source, source["source"]["id"])
    with db.session() as session:
        assert session.get(Source, source["source"]["id"]) is None


def test_conflicts_and_user_correction_survive_later_uploads(app, worker):
    _, client = app
    register(client)
    upload(client, "姓名：陈舟\n邮箱：first@example.test")
    upload(client, "姓名：陈舟\n邮箱：second@example.test")
    drain(worker)
    profile = client.get("/v1/profile").json()
    assert profile["fields"]["email"]["status"] == "conflict"
    claim = profile["fields"]["email"]["claims"][0]
    response = client.post(
        f"/v1/claims/{claim['id']}/decisions",
        json={"action": "correct", "expected_revision": claim["revision"], "value": "current@example.test"},
    )
    assert response.status_code == 200
    assert (
        client.post(
            f"/v1/claims/{claim['id']}/decisions",
            json={"action": "accept", "expected_revision": claim["revision"]},
        ).status_code
        == 409
    )
    upload(client, "邮箱：stale@example.test")
    drain(worker)
    assert client.get("/v1/profile").json()["fields"]["email"]["value"] == "current@example.test"


def test_revocation_invalidates_evidence_pages_and_drafts_then_cleans(app, worker):
    application, client = app
    user = register(client)
    imported = upload(client)
    drain(worker)
    sid = imported["source"]["id"]
    evidence = client.get(f"/v1/sources/{sid}/evidence").json()["items"][0]
    assert (
        client.post(
            "/v1/pages", json={"title": "个人概览", "content": "陈舟", "evidence_ids": [evidence["id"]]}
        ).status_code
        == 201
    )
    draft = client.post(
        "/v1/forms/drafts",
        json={"title": "表单", "fields": [{"key": "name", "label": "姓名", "fact_key": "name"}]},
    ).json()
    with application.state.db.session() as session:
        tenant_id = session.get(User, user["id"]).tenant_id
    with application.state.db.session(tenant_id) as session:
        original = object_path(application.state.settings, session.get(Source, sid).object_key)
    assert original.exists()
    assert client.delete(f"/v1/sources/{sid}").status_code == 202
    assert client.get(f"/v1/evidence/{evidence['id']}").status_code == 404
    assert client.get(f"/v1/sources/{sid}/original").status_code == 404
    assert not client.get("/v1/pages").json()["items"]
    changed = client.get(f"/v1/forms/drafts/{draft['id']}").json()
    assert changed["stale"] and changed["answers"] == []
    assert client.get("/v1/profile").json()["fields"]["name"]["status"] == "missing"
    drain(worker)
    assert not original.exists()
    with application.state.db.session(tenant_id) as session:
        assert session.scalar(select(Evidence).where(Evidence.source_id == sid)) is None
        assert session.get(Source, sid).status == "deleted"


def test_deleted_source_cannot_be_published_by_old_job(app, worker):
    _, client = app
    register(client)
    imported = upload(client)
    ticket = worker.claim()
    assert ticket and ticket["job_id"] == imported["job_id"]
    client.delete(f"/v1/sources/{imported['source']['id']}")
    worker.process(ticket)
    drain(worker)
    assert not client.get("/v1/sources").json()["items"]
    assert client.get("/v1/profile").json()["fields"]["name"]["status"] == "missing"


def test_expired_worker_cannot_publish(app, worker):
    application, client = app
    register(client)
    imported = upload(client)
    first = worker.claim()
    with worker.db.session(first["tenant_id"]) as session:
        session.get(Job, first["job_id"]).lease_until = now() - timedelta(seconds=1)
    second_worker = Worker(application.state.settings)
    try:
        second = second_worker.claim()
        assert second["fencing_token"] > first["fencing_token"]
        worker.process(first)
        assert client.get(f"/v1/sources/{imported['source']['id']}").json()["status"] == "queued"
        second_worker.process(second)
        assert client.get(f"/v1/sources/{imported['source']['id']}").json()["status"] == "ready"
    finally:
        second_worker.close()


def test_tokens_scope_revocation_and_csrf(app, worker):
    application, browser = app
    register(browser)
    upload(browser)
    drain(worker)
    missing = browser.post(
        "/v1/tokens",
        json={"label": "WorkBuddy", "scopes": ["knowledge:read"]},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert missing.status_code == 403
    token = browser.post("/v1/tokens", json={"label": "WorkBuddy", "scopes": ["knowledge:read"]}).json()
    with TestClient(application) as agent:
        agent.headers["Authorization"] = "Bearer " + token["token"]
        assert agent.get("/v1/sources").status_code == 200
        assert agent.get("/v1/profile").status_code == 403
        assert agent.post("/v1/sources/text", json={"title": "x", "text": "x"}).status_code == 403
        assert (
            agent.post("/v1/tokens", json={"label": "escalation", "scopes": ["knowledge:write"]}).status_code
            == 403
        )
        assert browser.delete(f"/v1/tokens/{token['id']}").status_code == 204
        assert agent.get("/v1/sources").status_code == 401
    assert browser.get("/v1/tokens").json()[0].get("token") is None


def test_agent_proposals_require_matching_original_evidence(app, worker):
    _, client = app
    register(client)
    imported = upload(client, "我的研究主题是分布式系统。")
    drain(worker)
    sid = imported["source"]["id"]
    evidence = client.get(f"/v1/sources/{sid}/evidence").json()["items"][0]
    body = {
        "source_id": sid,
        "expected_revision": 1,
        "items": [
            {
                "field": "research_interests",
                "value": "分布式系统",
                "quote": evidence["text"],
                "evidence_id": evidence["id"],
            }
        ],
    }
    accepted = client.post("/v1/claims/proposals", json=body)
    assert accepted.status_code == 201 and accepted.json()["status"] == "candidate"
    assert client.get("/v1/profile").json()["fields"]["research_interests"]["status"] == "missing"
    body["items"][0]["value"] = "人工智能专家"
    assert client.post("/v1/claims/proposals", json=body).status_code == 422


def test_unsupported_and_bad_files_are_not_marked_ready(app, worker):
    _, client = app
    register(client)
    for name, data in [("扫描.png", b"fake-image"), ("broken.pdf", b"not-pdf")]:
        assert client.post("/v1/sources/upload", files={"file": (name, data)}).status_code == 202
    drain(worker)
    states = {s["filename"]: s["status"] for s in client.get("/v1/sources").json()["items"]}
    assert states == {"扫描.png": "failed", "broken.pdf": "failed"}


def test_other_origin_rejected_and_tenant_body_not_accepted(app):
    _, client = app
    register(client)
    assert (
        client.post(
            "/v1/sources/text",
            headers={"Origin": "https://unrelated.example"},
            json={"title": "x", "text": "x"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/sources/text", json={"title": "x", "text": "x", "tenant_id": uuid4().hex}
        ).status_code
        == 422
    )


def test_abandoned_final_attempt_becomes_failed(app, worker):
    _, client = app
    register(client)
    imported = upload(client)
    ticket = worker.claim()
    with worker.db.session(ticket["tenant_id"]) as session:
        job = session.get(Job, ticket["job_id"])
        job.attempts = 5
        job.lease_until = now() - timedelta(seconds=1)
    # The source is exhausted, but its newly introduced directory task can still be claimed.
    next_ticket = worker.claim()
    if next_ticket:
        with worker.db.session(next_ticket["tenant_id"]) as session:
            assert session.get(Job, next_ticket["job_id"]).kind == "organize"
        worker.process(next_ticket)
    assert client.get(f"/v1/jobs/{imported['job_id']}").json()["status"] == "failed"
    assert client.get(f"/v1/sources/{imported['source']['id']}").json()["status"] == "failed"


def test_chunked_body_limit_before_json_parsing(app):
    _, client = app
    register(client)
    response = client.post(
        "/v1/sources/text",
        content=iter([b"x" * (1024 * 1024)] * 11),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_delete_during_parsing_does_not_publish(app, worker, monkeypatch):
    _, client = app
    register(client)
    imported = upload(client)
    ticket = worker.claim()
    started, release = Event(), Event()
    from akb import worker as module

    original = module.parse_isolated

    def delayed(filename, data):
        started.set()
        assert release.wait(5)
        return original(filename, data)

    monkeypatch.setattr(module, "parse_isolated", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        task = executor.submit(worker.process, ticket)
        assert started.wait(3)
        assert client.delete(f"/v1/sources/{imported['source']['id']}").status_code == 202
        release.set()
        task.result(timeout=10)
    assert client.get("/v1/profile").json()["fields"]["name"]["status"] == "missing"


def test_concurrent_decisions_require_current_revision(app, worker):
    _, client = app
    register(client)
    upload(client)
    drain(worker)
    claim = client.get("/v1/profile").json()["candidates"][0]

    def decide(action):
        return client.post(
            f"/v1/claims/{claim['id']}/decisions",
            json={"action": action, "expected_revision": claim["revision"]},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(decide, ["accept", "reject"]))
    assert sorted(results) == [200, 409]


def test_logout_revokes_browser_session_without_request_body(app):
    _, client = app
    register(client)
    assert client.post("/v1/auth/logout").status_code == 204
    assert client.get("/v1/auth/me").status_code == 401
