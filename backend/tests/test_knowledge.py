from datetime import timedelta
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import pytest
from akb.models import Job, KnowledgeTask, Source, now
from conftest import drain, register, upload
from docx import Document
from fastapi.testclient import TestClient


def source_task(client, worker, text, scope="self"):
    result = upload(client, text, scope=scope)
    drain(worker)
    source_id = result["source"]["id"]
    task = next(t for t in client.get("/v1/knowledge/tasks").json()["items"] if t["source_id"] == source_id)
    lease = client.post(f"/v1/knowledge/tasks/{task['id']}/claim").json()
    evidence = client.get(f"/v1/sources/{source_id}/evidence?limit=30").json()["items"]
    return lease, evidence


def fact(evidence, value, field="job_title", kind="employment", entity="current", **extra):
    return {
        "kind": kind,
        "subject": "李若晴",
        "subject_scope": "self",
        "entity_key": entity,
        "field": field,
        "value": value,
        "evidence_id": evidence["id"],
        "quote": evidence["text"],
        **extra,
    }


def submit(client, task, evidence, items, **extra):
    body = {
        "expected_revision": task["revision"],
        "request_id": str(uuid4()),
        "items": items,
        "complete": True,
        "reviewed_evidence_ids": [e["id"] for e in evidence],
        **extra,
    }
    response = client.post(f"/v1/knowledge/tasks/{task['id']}/submit", json=body)
    return response, body


def accept(client, ids):
    for fid in ids:
        response = client.post(
            f"/v1/knowledge/facts/{fid}/decisions", json={"action": "accept", "expected_revision": 1}
        )
        assert response.status_code == 200, response.text


def target(selector):
    operation = selector.get("operation", "value")
    numeric_units = {"amount_yuan": "元", "hours": "学时", "enrolments": "人次"}
    value_type = "integer" if operation == "count" else "number" if selector["field"] in numeric_units else "text"
    return {"knowledge": selector, "value_type": value_type,
            "unit": None if operation == "count" else numeric_units.get(selector["field"])}


def draft(client, field="administrative_position", selector=None):
    body = {
        "title": "教师申报",
        "fields": [
            {"key": field, "label": field, "fact_key": field, **({"knowledge": selector, "target": target(selector)} if selector else {})}
        ],
    }
    result = client.post("/v1/forms/drafts", json=body)
    assert result.status_code == 201, result.text
    return result.json()


def test_narrative_proposal_shared_catalog_and_unknown_negation(app, worker):
    _, client = app
    register(client)
    task, evidence = source_task(
        client, worker, "李若晴自2025年3月1日起聘任为副教授。\n李若晴不是系主任，行政任命另行通知。"
    )
    result, body = submit(
        client,
        task,
        evidence,
        [
            fact(evidence[0], "副教授", valid_from="2025-03-01", time_quote=evidence[0]["text"]),
            fact(evidence[1], "系主任", field="administrative_position", polarity="negative"),
        ],
    )
    assert result.status_code == 200, result.text
    assert draft(client, "job_title")["answers"][0]["status"] == "missing"
    before = client.get("/v1/knowledge").json()
    assert before["pending"] == {"tasks": 0, "candidates": 2, "conflicts": 0, "parse_failures": []}
    assert client.get("/v1/catalog").json()["knowledge"] == before
    assert client.post(f"/v1/knowledge/tasks/{task['id']}/submit", json=body).json() == result.json()
    assert client.get("/v1/knowledge").json()["revision"] == before["revision"]
    accept(client, result.json()["fact_ids"])
    assert draft(client, "job_title")["answers"][0]["value"] == "副教授"
    answer = draft(client)["answers"][0]
    assert answer["status"] == "missing" and answer["value"] is None
    assert answer["claims"][0]["polarity"] == "negative"
    assert answer["reason"] == "资料不足或只有局部否定，保持未知"


@pytest.mark.parametrize("polarity,value", [("absent", "无"), ("positive", "无"), ("positive", "系主任")])
def test_specific_denial_cannot_become_global_absence(app, worker, polarity, value):
    _, client = app
    register(client)
    task, evidence = source_task(client, worker, "李若晴不是系主任，行政任命另行通知。")
    result, _ = submit(
        client, task, evidence, [fact(evidence[0], value, field="administrative_position", polarity=polarity)]
    )
    assert result.status_code == 422
    assert client.get("/v1/knowledge").json()["total"] == 0


def test_explicit_absence_requires_confirmation(app, worker):
    _, client = app
    register(client)
    task, evidence = source_task(client, worker, "李若晴未担任任何行政职务。")
    result, _ = submit(
        client, task, evidence, [fact(evidence[0], "无", field="administrative_position", polarity="absent")]
    )
    assert result.status_code == 200, result.text
    accept(client, result.json()["fact_ids"])
    assert draft(client)["answers"][0]["value"] == "无"


def test_missing_documentation_does_not_prove_absence(app, worker):
    _, client = app
    register(client)
    task, evidence = source_task(client, worker, "李若晴暂无行政职务相关资料。")
    result, _ = submit(
        client, task, evidence, [fact(evidence[0], "无", field="administrative_position", polarity="absent")]
    )
    assert result.status_code == 422


def test_word_uses_same_draft_preserves_manual_runs_and_rejects_stale(app, worker):
    _, client = app
    register(client)
    task, es = source_task(client, worker, "李若晴担任副教授。")
    result, _ = submit(client, task, es, [fact(es[0], "副教授")])
    assert result.status_code == 200
    accept(client, result.json()["fact_ids"])
    prepared = client.post(
        "/v1/forms/drafts",
        json={
            "title": "教师登记",
            "fields": [
                {
                    "key": "职称",
                    "label": "职称",
                    "fact_key": "job_title",
                    "knowledge": {"kind": "employment", "field": "job_title", "as_of": "2026-10-17"},
                    "target": target({"kind": "employment", "field": "job_title", "as_of": "2026-10-17"}),
                },
                {"key": "role", "label": "行政职务", "fact_key": "administrative_position"},
            ],
        },
    ).json()
    doc = Document()
    p = doc.add_paragraph("职称：")
    p.add_run("{{field:").bold = True
    p.add_run("职称}}；行政职务：{{field:role}}")
    note = "人工备注：先核对材料，不得改写本段。"
    doc.add_paragraph(note)
    template = BytesIO()
    doc.save(template)
    response = client.post(
        f"/v1/forms/drafts/{prepared['id']}/document", files={"file": ("申请表.docx", template.getvalue())}
    )
    assert response.status_code == 200, response.text
    filled = Document(BytesIO(response.content))
    assert filled.paragraphs[0].text == "职称：副教授；行政职务："
    assert filled.paragraphs[1].text == note
    assert filled.paragraphs[0].runs[1].bold is True
    with ZipFile(template) as before, ZipFile(BytesIO(response.content)) as after:
        assert all(before.read(n) == after.read(n) for n in before.namelist() if n != "word/document.xml")
    revision = client.get("/v1/knowledge").json()["revision"]
    page = client.post(
        "/v1/pages",
        json={
            "title": "个人介绍",
            "content": "副教授",
            "evidence_ids": [es[0]["id"]],
            "fact_ids": result.json()["fact_ids"],
            "expected_revision": revision,
        },
    )
    assert page.status_code == 201
    assert not client.get("/v1/pages").json()["items"][0]["stale"]
    client.post(
        f"/v1/knowledge/facts/{result.json()['fact_ids'][0]}/decisions",
        json={"action": "correct", "expected_revision": 2, "value": "教授"},
    )
    stale = client.post(
        f"/v1/forms/drafts/{prepared['id']}/document", files={"file": ("申请表.docx", template.getvalue())}
    )
    assert stale.status_code == 409
    page = client.get("/v1/pages").json()["items"][0]
    assert page["stale"] and not page["content"]


def test_incremental_versions_reject_stale_task_preserve_manual_and_invalidate_draft(app, worker):
    application, client = app
    register(client)
    task, evidence = source_task(client, worker, "李若晴的工作单位为澄川学院。")
    result, _ = submit(client, task, evidence, [fact(evidence[0], "澄川学院", field="organization")])
    assert result.status_code == 200
    fid = result.json()["fact_ids"][0]
    response = client.post(
        f"/v1/knowledge/facts/{fid}/decisions",
        json={"action": "correct", "value": "澄川大学", "expected_revision": 1},
    )
    assert response.status_code == 200
    prepared = draft(client, "organization")
    assert prepared["answers"][0]["value"] == "澄川大学"
    tenant_id = client.get("/v1/context").json()["knowledge_base_id"]
    # Simulate a new evidence generation on the stable source, as native sync does.
    with application.state.db.session(tenant_id) as session:
        source = session.get(Source, task["source_id"])
        source.revision += 1
        source.status = "queued"
        session.add(Job(tenant_id=tenant_id, source_id=source.id, source_revision=source.revision))
    drain(worker)
    assert client.get(f"/v1/forms/drafts/{prepared['id']}").json()["stale"]
    assert draft(client, "organization")["answers"][0]["value"] == "澄川大学"
    assert client.get("/v1/knowledge").json()["pending"]["tasks"] == 1
    old, _ = submit(client, task, evidence, [])
    assert old.status_code == 409
    assert client.delete(f"/v1/sources/{task['source_id']}").status_code == 202
    drain(worker)
    assert draft(client, "organization")["answers"][0]["value"] == "澄川大学"
    assert all(not f["source_id"] for f in client.get("/v1/knowledge").json()["items"])


def test_current_dates_conflict_and_deterministic_dedup_sum(app, worker):
    _, client = app
    register(client)
    for text, value, start in [
        ("李若晴自2021年9月1日起聘任为讲师。", "讲师", "2021-09-01"),
        ("李若晴自2025年3月1日起聘任为副教授。", "副教授", "2025-03-01"),
    ]:
        task, es = source_task(client, worker, text)
        r, _ = submit(client, task, es, [fact(es[0], value, valid_from=start, time_quote=text)])
        assert r.status_code == 200, r.text
        accept(client, r.json()["fact_ids"])
    assert draft(client, "job_title")["answers"][0]["value"] == "副教授"
    view = client.get("/v1/knowledge").json()
    assert next(f for f in view["items"] if f["value"] == "讲师")["effective"] is False
    assert next(f for f in view["items"] if f["value"] == "副教授")["effective"] is True
    for text, entity, value in [
        ("李若晴2025年春季课程学时为48学时。", "2025春课程", "48学时"),
        ("李若晴2025年春季课程核对记录：48学时。", "2025 春课程", "48学时"),
        ("李若晴2025年秋季课程学时为32学时。", "2025秋课程", "32学时"),
    ]:
        task, es = source_task(client, worker, text)
        r, _ = submit(
            client,
            task,
            es,
            [
                fact(es[0], value, kind="teaching", field="hours", entity=entity),
                fact(es[0], "2025", kind="teaching", field="year", entity=entity),
            ],
        )
        assert r.status_code == 200, r.text
        accept(client, r.json()["fact_ids"])
    selector = {"kind": "teaching", "field": "hours", "operation": "sum", "filters": {"year": "2025"}}
    answer = draft(client, "total_hours", selector)["answers"][0]
    assert answer["value"] == "80" and answer["calculation"]["record_count"] == 2
    assert len(answer["evidence_ids"]) == 3


def test_scope_lease_permissions_and_identity_binding(app, worker):
    application, client = app
    register(client)
    task, es = source_task(client, worker, "李若晴在外校担任教授。", scope="reference")
    denied, _ = submit(client, task, es, [fact(es[0], "教授")])
    assert denied.status_code == 422
    proposal = fact(es[0], "教授", subject_scope="reference")
    result, _ = submit(client, task, es, [proposal])
    assert result.status_code == 200
    accept(client, result.json()["fact_ids"])
    assert draft(client, "job_title")["answers"][0]["status"] == "missing"
    token = client.post(
        "/v1/tokens", json={"label": "Agent", "scopes": ["knowledge:read", "knowledge:write", "profile:read"]}
    ).json()["token"]
    with TestClient(application) as agent:
        agent.headers["Authorization"] = "Bearer " + token
        assert (
            agent.post(
                f"/v1/knowledge/facts/{result.json()['fact_ids'][0]}/decisions",
                json={"action": "accept", "expected_revision": 2},
            ).status_code
            == 403
        )
        agent.headers["X-AKB-Knowledge-Base"] = str(uuid4())
        assert agent.get("/v1/sources").status_code == 409
        assert agent.post("/v1/sources/text", json={"title": "错库", "text": "不应落库"}).status_code == 409
    with TestClient(application) as other:
        register(other)
        assert other.get("/v1/knowledge").json()["total"] == 0
        assert other.post(f"/v1/knowledge/tasks/{task['id']}/claim").status_code == 409


def test_expired_task_reclaimed_partial_read_and_no_duplicate_refresh(app, worker):
    application, client = app
    register(client)
    task, es = source_task(client, worker, "李若晴担任副教授。\n李若晴的研究方向是计算机视觉。")
    r, _ = submit(client, task, es[:1], [fact(es[0], "副教授")])
    assert r.status_code == 422
    tenant_id = client.get("/v1/context").json()["knowledge_base_id"]
    with application.state.db.session(tenant_id) as session:
        session.get(KnowledgeTask, task["id"]).lease_until = now() - timedelta(seconds=1)
    r, _ = submit(client, task, es, [fact(es[0], "副教授")])
    assert r.status_code == 409
    new = client.post(f"/v1/knowledge/tasks/{task['id']}/claim").json()
    r, _ = submit(client, new, es, [fact(es[0], "副教授")])
    assert r.status_code == 200
    before = client.get("/v1/catalog").json()
    client.post("/v1/catalog/organize")
    drain(worker)
    after = client.get("/v1/catalog").json()
    assert after["revision"] == before["revision"]
    assert after["knowledge"]["pending"]["tasks"] == 0


def test_confirmed_conflicts_block_form_and_appear_in_shared_directory(app, worker):
    _, client = app
    register(client)
    for title in ["教授", "副教授"]:
        task, es = source_task(client, worker, f"李若晴担任{title}。")
        result, _ = submit(client, task, es, [fact(es[0], title, entity=title)])
        assert result.status_code == 200
        accept(client, result.json()["fact_ids"])
    view = client.get("/v1/knowledge").json()
    assert view["pending"]["conflicts"] == 1
    assert all(f["conflict"] for f in view["items"])
    assert "有冲突，不用于填写" in view["markdown"]
    answer = draft(client, "job_title")["answers"][0]
    assert answer["status"] == "conflict" and answer["value"] is None


def test_explicit_absence_is_not_an_achievement_count(app, worker):
    _, client = app
    register(client)
    task, es = source_task(client, worker, "李若晴无荣誉名称。")
    result, _ = submit(
        client, task, es, [fact(es[0], "无", kind="award", field="title", polarity="absent")]
    )
    assert result.status_code == 200
    accept(client, result.json()["fact_ids"])
    answer = draft(client, "awards", {"kind": "award", "field": "title", "operation": "count"})["answers"][0]
    assert answer["status"] == "missing" and answer["value"] is None
    assert answer["calculation"]["record_count"] == 0


def test_agent_lease_owner_and_revision_fence_replayed_submissions(app, worker):
    application, client = app
    register(client)
    task, es = source_task(client, worker, "李若晴担任教授。")
    tenant_id = client.get("/v1/context").json()["knowledge_base_id"]
    token = client.post(
        "/v1/tokens", json={"label": "另一个 Agent", "scopes": ["knowledge:read", "knowledge:write"]}
    ).json()["token"]
    with TestClient(application) as agent:
        agent.headers["Authorization"] = "Bearer " + token
        assert agent.post(f"/v1/knowledge/tasks/{task['id']}/claim").status_code == 409
        with application.state.db.session(tenant_id) as session:
            session.get(KnowledgeTask, task["id"]).lease_until = now() - timedelta(seconds=1)
        new = agent.post(f"/v1/knowledge/tasks/{task['id']}/claim").json()
        old, _ = submit(client, task, es, [fact(es[0], "教授")])
        assert old.status_code == 409
        result, body = submit(agent, new, es, [fact(es[0], "教授")])
        assert result.status_code == 200
        assert client.post(f"/v1/knowledge/tasks/{task['id']}/submit", json=body).status_code == 409
        body["note"] = "相同请求标识但改变内容"
        assert agent.post(f"/v1/knowledge/tasks/{task['id']}/submit", json=body).status_code == 409
