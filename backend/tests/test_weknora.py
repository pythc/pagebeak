"""Actual native HTTP storage plus PostgreSQL fences. No substitute LLM answers."""

import os
import subprocess
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from akb.api import create_app
from akb.config import ROOT
from akb.models import AgentJob, Evidence, Source, now
from akb.worker import Worker
from conftest import drain, register, upload
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.skipif(
    os.getenv("AKB_TEST_WEKNORA") != "1", reason="requires pinned external-mode WeKnora"
)


@pytest.fixture(scope="module")
def native_settings(test_settings):
    name = "akb_weknora_test"
    admin = make_url(test_settings.migration_database_url).set(drivername="postgresql", database="postgres")
    with psycopg.connect(admin.render_as_string(hide_password=False), autocommit=True) as connection:
        if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
            connection.execute("CREATE DATABASE akb_weknora_test")
    urls = {
        key: make_url(getattr(test_settings, key)).set(database=name).render_as_string(hide_password=False)
        for key in ("database_url", "worker_database_url", "migration_database_url")
    }
    subprocess.run(
        [str(ROOT / ".venv/bin/alembic"), "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "AKB_MIGRATION_DATABASE_URL": urls["migration_database_url"]},
        capture_output=True,
        check=True,
    )
    return test_settings.model_copy(update=urls)


@pytest.fixture
def native_app(native_settings, tmp_path):
    # Use isolated test credentials and AKB test DB; never production tenants.
    settings = native_settings.model_copy(
        update={
            "knowledge_engine": "weknora",
            "storage_dir": tmp_path / "objects",
            "weknora_secrets_dir": tmp_path / "secrets",
            "native_sync_enabled": False,
        }
    )
    app = create_app(settings)
    worker = Worker(settings)
    # Ignore foreign test jobs by processing only tickets for this fixture below.
    with TestClient(app) as browser:
        register(browser)
        tenant = browser.get("/v1/context").json()["knowledge_base_id"]
        token = browser.post(
            "/v1/tokens",
            json={"label": "weknora-test", "scopes": ["knowledge:read", "knowledge:write", "profile:read"]},
        ).json()["token"]
        with TestClient(app) as agent:
            agent.headers.update({"Authorization": "Bearer " + token, "X-AKB-Knowledge-Base": tenant})
            yield app, browser, agent, worker, tenant
    worker.close()


def submitted(agent, path, body=None):
    r = agent.post(path, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def pending_fixture(native_app):
    app, browser, agent, worker, tenant = native_app
    src = upload(browser, "陈沐岚在2026年主持青竹项目，批准经费128000元，到账96000元；尚未结题。")
    drain(worker)
    claimed = submitted(agent, "/v2/organization/jobs/claim")
    assert claimed["job"], agent.get("/v2/library").text
    sid = src["source"]["id"]
    version = claimed["job"]["source_version"]
    with app.state.db.session(tenant) as session:
        ev = session.scalar(select(Evidence).where(Evidence.source_id == sid))
    item = {
        "request_id": str(uuid4()),
        "kind": "page",
        "key": "qingzhu",
        "title": "青竹项目",
        "content": "陈沐岚主持青竹项目，批准12.8万元，到账9.6万元，尚未结题。",
        "expected_revision": 0,
        "source_versions": {sid: version},
        "citations": [
            {
                "statement": "尚未结题",
                "evidence": {"evidence_id": ev.id, "source_version": version, "quote": "尚未结题"},
            }
        ],
    }
    return claimed, item, sid, ev


def test_native_index_jobs_manual_protection_incremental_and_filling(native_app):
    app, browser, agent, worker, tenant = native_app
    claim, item, sid, ev = pending_fixture(native_app)
    # Native retrieval must work before the model has made any summary.
    hits = submitted(agent, "/v2/library/search", {"query": "青竹"})["items"]
    assert any(h.get("source_id") == sid for h in hits)
    path = f"/v2/organization/jobs/{claim['job']['id']}/complete"
    body = {"lease_token": claim["lease_token"], "items": [item]}
    result = submitted(agent, path, body)
    assert result["state"] == "completed"
    assert submitted(agent, path, body)["replayed"]
    pageid = result["result_ids"][0]
    assert agent.get(f"/v2/library/objects/{pageid}").json()["content"] == item["content"]
    assert submitted(agent, "/v2/library/search", {"query": "青竹"})["items"]
    assert agent.get("/v2/library").json()["pending"] == 0
    # Human edits are protected regardless of model instructions.
    edited = item | {
        "request_id": str(uuid4()),
        "expected_revision": 1,
        "content": item["content"] + "\n人工备注：须附财务凭证。",
    }
    assert submitted(browser, "/v2/library/objects", edited)["protected"]
    assert (
        agent.post(
            "/v2/library/objects", json=edited | {"request_id": str(uuid4()), "expected_revision": 2}
        ).status_code
        == 409
    )
    version = claim["job"]["source_version"]
    plan = {
        "request_id": str(uuid4()),
        "fields": [
            {
                "key": "approved",
                "label": "批准经费",
                "operation": "sum",
                "value_type": "number",
                "unit": "万元",
                "rows": [
                    {
                        "entity_key": "青竹项目",
                        "value": "128000",
                        "unit": "元",
                        "attributes": {},
                        "evidence": {"evidence_id": ev.id, "source_version": version, "quote": ev.text},
                    }
                ],
            }
        ],
    }
    receipt = submitted(agent, "/v2/filling/prepare", plan)
    assert receipt["values"]["approved"] == "12.8"
    # New generation invalidates old task and old page and produces new work.
    with app.state.db.session(tenant) as session:
        session.get(Source, sid).revision += 1
    submitted(agent, "/v2/library/sync")
    drain(worker)
    stale = agent.get(f"/v2/library/objects/{pageid}").json()
    assert not stale["current"] and stale["protected"] and "人工备注" in stale["historical_content"]
    assert agent.post(path, json=body).status_code == 409
    nextjob = submitted(agent, "/v2/organization/jobs/claim")
    assert nextjob["job"]["source_version"] != version


def test_lease_expiry_rotation_foreign_tenant_and_forged_completion(native_app):
    app, browser, agent, worker, tenant = native_app
    claim, item, sid, ev = pending_fixture(native_app)
    jobid = claim["job"]["id"]
    path = f"/v2/organization/jobs/{jobid}/complete"
    invalid = {"lease_token": claim["lease_token"], "items": [item | {"citations": []}]}
    assert agent.post(path, json=invalid).status_code == 422
    with app.state.db.session(tenant) as session:
        session.get(AgentJob, jobid).lease_until = now() - timedelta(seconds=1)
    assert agent.post(path, json={"lease_token": claim["lease_token"], "items": [item]}).status_code == 409
    fresh = submitted(agent, "/v2/organization/jobs/claim")
    assert fresh["job"]["id"] == jobid and fresh["lease_token"] != claim["lease_token"]
    assert agent.post(path, json={"lease_token": claim["lease_token"], "items": [item]}).status_code == 409
    with TestClient(app) as other:
        register(other)
        assert (
            other.post(path, json={"lease_token": fresh["lease_token"], "items": [item]}).status_code == 404
        )
        assert other.get("/v2/library").json()["items"] == []
    submitted(agent, f"/v2/organization/jobs/{jobid}/renew", {"lease_token": fresh["lease_token"]})
    submitted(
        agent,
        f"/v2/organization/jobs/{jobid}/release",
        {"lease_token": fresh["lease_token"], "reason": "interrupted"},
    )
    assert agent.get("/v2/organization/jobs").json()["items"][0]["state"] == "pending"


def test_changed_dependency_requeues_unchanged_source_without_overwriting_manual_page(native_app):
    app, browser, agent, worker, tenant = native_app
    claim, item, sid, ev = pending_fixture(native_app)
    other = upload(browser, "青竹项目补充说明：结题材料正在准备。")
    drain(worker)
    other_id = other["source"]["id"]
    from akb.memory_library import source_version
    from akb.models import MemoryObject

    with app.state.db.session(tenant) as session:
        other_version = source_version(session.get(Source, other_id))
    item["source_versions"][other_id] = other_version
    path = f"/v2/organization/jobs/{claim['job']['id']}/complete"
    body = {"lease_token": claim["lease_token"], "items": [item]}
    result = submitted(agent, path, body)
    page_id = result["result_ids"][0]
    edited = item | {"request_id": str(uuid4()), "expected_revision": 1,
                     "content": item["content"] + "\n人工备注：保留项目名称。"}
    submitted(browser, "/v2/library/objects", edited)
    with app.state.db.session(tenant) as session:
        session.get(Source, other_id).revision += 1
    jobs = agent.get("/v2/organization/jobs").json()["items"]
    job = next(j for j in jobs if j["id"] == claim["job"]["id"])
    assert job["state"] == "pending" and job["error"] == "dependent_source_changed"
    assert job["source_version"] == claim["job"]["source_version"]
    stale = agent.get(f"/v2/library/objects/{page_id}").json()
    assert stale["protected"] and "人工备注" in stale["historical_content"]
    assert agent.post(path, json=body).status_code == 409
    reclaimed = submitted(agent, "/v2/organization/jobs/claim")
    assert reclaimed["job"]["id"] == job["id"]
    fresh_item = item | {"request_id": str(uuid4()), "key": "qingzhu_reconciled",
                         "expected_revision": 0, "source_versions": {sid: claim["job"]["source_version"]}}
    completed = submitted(agent, path, {"lease_token": reclaimed["lease_token"], "items": [fresh_item]})
    assert completed["error"] is None
    assert next(j for j in agent.get("/v2/organization/jobs").json()["items"] if j["id"] == job["id"])["state"] == "completed"
    # Forgetting the replacement is intentional; do not recreate it automatically.
    with app.state.db.session(tenant) as session:
        replacement = session.scalar(select(MemoryObject).where(MemoryObject.key == "qingzhu_reconciled"))
        replacement.deleted = True
    assert next(j for j in agent.get("/v2/organization/jobs").json()["items"] if j["id"] == job["id"])["state"] == "completed"


def test_engine_binding_and_offline_never_fall_back(native_app):
    app, browser, agent, worker, tenant = native_app
    claim, item, sid, ev = pending_fixture(native_app)
    from akb.models import MemoryBinding

    with app.state.db.session(tenant) as session:
        session.scalar(select(MemoryBinding)).engine = "tencent"
    assert agent.get("/v2/library").status_code == 409
    with app.state.db.session(tenant) as session:
        session.scalar(select(MemoryBinding)).engine = "weknora"
    app.state.settings.weknora_url = "http://127.0.0.1:1"
    result = agent.post("/v2/library/search", json={"query": "青竹"})
    assert result.status_code == 503
    assert agent.get("/v2/organization/jobs").json()["items"][0]["state"] == "leased"


def test_office_and_cpu_ocr_ingest_without_model_config(native_app, tmp_path):
    from docx import Document
    from openpyxl import Workbook
    from PIL import Image, ImageDraw, ImageFont
    from pptx import Presentation

    app, browser, agent, worker, tenant = native_app
    doc = Document()
    doc.add_paragraph("教师陈沐岚负责青竹课题，工作时间2026年。")
    doc.save(tmp_path / "聘任.docx")
    book = Workbook()
    book.active.append(["课题", "批准金额（元）"])
    book.active.append(["青竹", 128000])
    book.save(tmp_path / "经费.xlsx")
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "青竹课题进展"
    slide.placeholders[1].text = "阶段验收完成，最终结题未完成。"
    deck.save(tmp_path / "汇报.pptx")
    image = Image.new("RGB", (1400, 250), "white")
    ImageDraw.Draw(image).text(
        (50, 60), "Qingzhu Project Approved 128000", fill="black", font=ImageFont.load_default(size=48)
    )
    image.save(tmp_path / "扫描.png")
    image.save(tmp_path / "扫描.pdf", "PDF", resolution=150)
    for path in sorted(tmp_path.iterdir()):
        if path.suffix not in {".docx", ".xlsx", ".pptx", ".png", ".pdf"}:
            continue
        result = browser.post(
            "/v1/sources/upload",
            files={"file": (path.name, path.read_bytes())},
            data={"subject_scope": "self"},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert result.status_code == 202, result.text
    drain(worker)
    catalog = agent.get("/v2/library").json()
    assert len(catalog["sources"]) == 5
    assert len(agent.get("/v2/organization/jobs").json()["items"]) == 5
    assert any(s["coverage"] == "partial" for s in catalog["sources"] if s["title"].endswith(".pdf"))
    assert submitted(agent, "/v2/library/search", {"query": "Qingzhu"})["items"]
    assert submitted(agent, "/v2/library/search", {"query": "青竹"})["items"]
