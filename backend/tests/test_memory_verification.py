"""Meaningful failure cases for source arithmetic, provenance and native recovery."""

import os
from copy import deepcopy
from uuid import uuid4

import pytest
from akb import memory_library, memory_runtime
from akb.memory_filling import convert, number
from akb.models import MemoryBinding, MemoryObject, Tenant
from akb.tencent_client import TencentClient
from conftest import drain, upload
from fastapi import HTTPException
from sqlalchemy import select
from test_tencent_memory import item, publish  # noqa: F401
from test_tencent_memory import memory_app as memory_app

live = pytest.mark.skipif(os.getenv("AKB_TEST_TENCENT") != "1", reason="requires real Tencent services")


@pytest.mark.parametrize("value", ["1,23", "NaN", "1e9", "约5", "1-3", "12元", ".5", "9" * 51])
def test_reject_ambiguous_numbers(value):
    with pytest.raises(HTTPException):
        number(value)


def test_decimal_conversion_and_incompatible_units():
    assert str(convert(number("82,431.25"), "元", "万元")) == "8.243125"
    with pytest.raises(HTTPException):
        convert(number("8"), "美元", "元")


def source_rows(agent, source_id):
    lib = agent.get("/v2/library").json()
    version = next(s["version"] for s in lib["sources"] if s["id"] == source_id)
    raw = agent.get(f"/v1/sources/{source_id}/content").json()
    return version, raw


def setup_rows(browser, agent, worker):
    data = upload(
        browser,
        "P25A：2025年，林澈主持，批准经费82431.25元。\nP25B：2025年，林澈参与，批准经费90000元。\nP26C：2026年，林澈主持，批准经费500000元。",
    )
    drain(worker)
    sid = data["source"]["id"]
    version = next(s["version"] for s in agent.get("/v2/library").json()["sources"] if s["id"] == sid)
    # Use the same original API as agents; query evidence via authenticated context below.
    return sid, version


def get_evidence(app, tenant, sid):
    from akb.models import Evidence

    with app.state.db.session(tenant) as session:
        return list(session.scalars(select(Evidence).where(Evidence.source_id == sid)))


@live
def test_compute_dedup_contract_readback_and_new_source_invalidation(memory_app):
    app, browser, agent, worker, tenant = memory_app
    sid, version = setup_rows(browser, agent, worker)
    evs = get_evidence(app, tenant, sid)
    text = "\n".join(e.text for e in evs)

    def row(key, val, year, role):
        quote = next(line for line in text.splitlines() if key in line)
        ev = next(e for e in evs if quote in e.text)
        return {
            "entity_key": key,
            "value": val,
            "unit": "元",
            "attributes": {"year": year, "role": role},
            "evidence": {"evidence_id": ev.id, "source_version": version, "quote": quote},
        }

    first = row("P25A", "82431.25", "2025", "主持")
    field = {
        "key": "amount",
        "label": "2025年主持项目批准经费",
        "operation": "sum",
        "value_type": "number",
        "unit": "万元",
        "filters": {"year": "2025", "role": "主持"},
        "rows": [first, first, row("P25B", "90000", "2025", "参与"), row("P26C", "500000", "2026", "主持")],
    }
    plan = {
        "request_id": str(uuid4()),
        "fields": [
            field,
            {"key": "phone", "label": "电话", "operation": "unknown", "missing_reason": "全部资料未记载"},
            {"key": "narrative", "label": "事迹", "operation": "compose", "field_keys": ["amount"]},
        ],
    }
    r = agent.post("/v2/filling/prepare", json=plan)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["values"] == {
        "amount": "8.243125",
        "phone": "",
        "narrative": "2025年主持项目批准经费：8.243125万元。",
    }
    assert (
        data["details"]["amount"]["duplicates"] == ["P25A"]
        and len(data["details"]["amount"]["excluded"]) == 2
    )
    assert agent.post("/v2/filling/prepare", json=plan).json()["receipt_id"] == data["receipt_id"]
    path = f"/v2/filling/{data['receipt_id']}/validate"
    assert agent.post(path, json={"values": data["values"], "artifact": "web_readback"}).json()["valid"]
    assert not agent.post(
        path, json={"values": {**data["values"], "amount": "8.24"}, "artifact": "word_readback"}
    ).json()["valid"]
    assert not agent.post(path, json={"values": {"amount": "8.243125"}, "artifact": "draft"}).json()["valid"]
    wrong = deepcopy(plan)
    wrong["request_id"] = str(uuid4())
    wrong["fields"][0]["value_type"] = "text"
    assert agent.post("/v2/filling/prepare", json=wrong).status_code == 422
    wrong = deepcopy(plan)
    wrong["request_id"] = str(uuid4())
    wrong["fields"][0]["rows"][0]["value"] = "2431.25"
    assert agent.post("/v2/filling/prepare", json=wrong).status_code == 422
    wrong = deepcopy(plan)
    wrong["request_id"] = str(uuid4())
    wrong["fields"][0]["rows"][0]["attributes"]["role"] = None
    assert agent.post("/v2/filling/prepare", json=wrong).status_code == 422
    wrong = deepcopy(plan)
    wrong["fields"][0]["unit"] = "元"
    assert agent.post("/v2/filling/prepare", json=wrong).status_code == 409
    from conftest import register
    from fastapi.testclient import TestClient

    with TestClient(app) as other:
        register(other)
        assert other.post(path, json={"values": data["values"], "artifact": "draft"}).status_code == 404
    upload(browser, "追加人事通知：林澈于2026年受聘教授。")
    # Upload alone invalidates the receipt; do not wait until the worker parses it.
    assert agent.post(path, json={"values": data["values"], "artifact": "draft"}).status_code == 409
    drain(worker)
    assert agent.post(path, json={"values": data["values"], "artifact": "draft"}).status_code == 409


@live
def test_exact_provenance_unicode_duplicates_and_human_edits(memory_app):
    app, browser, agent, worker, tenant = memory_app
    data = upload(
        browser, "证书：阿里云\u200bApsara Clouder，Efficient\u2011Net。\n说明：任职讲师。任职讲师。"
    )
    drain(worker)
    sid = data["source"]["id"]
    evs = get_evidence(app, tenant, sid)
    version = next(s["version"] for s in agent.get("/v2/library").json()["sources"] if s["id"] == sid)
    ev = next(e for e in evs if "Apsara" in e.text)
    anchor = {"evidence_id": ev.id, "source_version": version, "quote": "阿里云Apsara Clouder"}
    resolved = agent.post("/v2/library/evidence/resolve", json=anchor)
    assert resolved.status_code == 200, resolved.text
    assert "\u200b" in resolved.json()["quote"] and resolved.json()["match"] == "normalized"
    body = item(
        "page",
        key="certificate",
        title="证书记录",
        content="证书名称：阿里云Apsara Clouder。",
        source_versions={sid: version},
        citations=[{"statement": "证书名称：阿里云Apsara Clouder。", "evidence": anchor}],
    )
    saved = publish(agent, body)
    read = agent.get(f"/v2/library/objects/{saved['id']}").json()
    assert read["citation_count"] == 1 and "\u200b" in read["citations"][0]["quote"]
    assert read["citations"][0]["locator"] and read["citations"][0]["statement"] == body["content"]
    repeated = next(e for e in evs if e.text.count("任职讲师") > 1)
    assert (
        agent.post(
            "/v2/library/evidence/resolve", json={**anchor, "evidence_id": repeated.id, "quote": "任职讲师"}
        ).status_code
        == 422
    )
    modified = {
        **body,
        "request_id": str(uuid4()),
        "expected_revision": 1,
        "content": "人工更正：待复核证书全称。",
        "citations": [],
    }
    manual = publish(browser, modified)
    assert manual["protected"] and manual["citation_count"] == 0
    with app.state.db.session(tenant) as session:
        from akb.models import Source

        session.get(Source, sid).revision += 1
    assert agent.post("/v2/library/evidence/resolve", json=anchor).status_code == 409


@live
def test_short_names_quota_orphan_recovery_and_checksum(memory_app, monkeypatch):
    app, browser, agent, worker, tenant = memory_app
    body = item(content="资料集简称为青禾，文风先列结论。")
    saved = publish(agent, body)
    hits = agent.post("/v2/library/search", json={"query": "青禾"}).json()["items"]
    assert any(h["id"] == saved["id"] and h.get("match") == "literal" for h in hits)
    usage = agent.get("/v1/storage").json()  # This limited token has knowledge:read.
    assert (
        usage["memory_bytes"] > 0 and usage["used_bytes"] == usage["original_bytes"] + usage["memory_bytes"]
    )
    settings = app.state.settings
    with app.state.db.session(tenant) as session:
        owner = session.get(Tenant, tenant)
        original_quota = owner.quota_bytes
        owner.quota_bytes = owner.memory_bytes + 10
    assert agent.post("/v2/library/objects", json=item(key="full", content="新内容" * 20)).status_code == 413
    with app.state.db.session(tenant) as session:
        session.get(Tenant, tenant).quota_bytes = original_quota
    original = TencentClient.call

    def fail_after_write(self, component, path, body=None):
        result = original(self, component, path, body)
        if path == "/v3/conversation/add":
            raise HTTPException(503, "simulated uncertain response after native commit")
        return result

    monkeypatch.setattr(TencentClient, "call", fail_after_write)
    assert (
        agent.post("/v2/library/objects", json=item(key="orphan", content="尚未提交的远端正文")).status_code
        == 503
    )
    monkeypatch.setattr(TencentClient, "call", original)
    with app.state.db.session(tenant) as session:
        before, _ = memory_runtime.inventory(session, settings, tenant)
        assert len(before["sessions"]) == 2
        memory_library.maintain(session, settings, tenant)
    with app.state.db.session(tenant) as session:
        after, _ = memory_runtime.inventory(session, settings, tenant)
        assert len(after["sessions"]) == 1
    assert agent.get(f"/v2/library/objects/{saved['id']}").json()["content"] == body["content"]
    # Pre-journal orphan discovery, independent of request logs.
    orphan = "zhixu-" + str(uuid4()) + "-1-0123456789ab"
    native = TencentClient(settings, tenant)
    native.call(
        "core",
        "/v3/conversation/add",
        {"session_id": orphan, "messages": [{"role": "assistant", "content": "orphan"}]},
    )
    with app.state.db.session(tenant) as session:
        memory_library.maintain(session, settings, tenant)
    assert native.call("core", "/v3/conversation/query", {"session_id": orphan, "limit": 100})["total"] == 0
    sid, version = setup_rows(browser, agent, worker)
    page = publish(agent, item("page", key="page", content="项目资料。", source_versions={sid: version}))
    with app.state.db.session(tenant) as session:
        obj = session.get(MemoryObject, page["id"])
        state = session.scalar(select(MemoryBinding))
        native.call(
            "wiki",
            "/v3/zhixu/publish",
            {"wiki_id": state.wiki_id, "pages": [{"ref": obj.remote_ref, "content": "篡改正文"}]},
        )
    assert agent.get(f"/v2/library/objects/{page['id']}").status_code == 503


@live
def test_raw_update_keeps_protected_history_and_uncertain_export_is_cleaned(memory_app, monkeypatch):
    app, browser, agent, worker, tenant = memory_app
    sid, version = setup_rows(browser, agent, worker)
    page = publish(
        browser,
        item(
            "page",
            key="manual-history",
            content="人工决定：保留项目的历史批准额。",
            source_versions={sid: version},
        ),
    )
    with app.state.db.session(tenant) as session:
        state = session.scalar(select(MemoryBinding))
        obj = session.get(MemoryObject, page["id"])
        memory_runtime.journal(
            session, app.state.settings, tenant, state.wiki_id, "raw", obj.raw_refs[0]
        )
        # Simulate a successful SQL commit whose post-commit journal unlink failed.
        session.info["published_journals"] = []
    with app.state.db.session(tenant) as session:
        from akb.models import Source

        session.get(Source, sid).revision += 1
        memory_library.sync_sources(session, app.state.settings, tenant)
    with app.state.db.session(tenant) as session:
        memory_library.maintain(session, app.state.settings, tenant)
    result = agent.get(f"/v2/library/objects/{page['id']}")
    assert result.status_code == 200, result.text
    assert result.json()["historical_content"] == "人工决定：保留项目的历史批准额。"
    assert not result.json()["current"]
    original = TencentClient.call

    def uncertain(self, component, path, body=None):
        result = original(self, component, path, body)
        if path == "/v3/wiki/raw/write":
            raise HTTPException(503, "simulated crash after raw write")
        return result

    monkeypatch.setattr(TencentClient, "call", uncertain)
    with pytest.raises(HTTPException):
        with app.state.db.session(tenant) as session:
            session.get(Source, sid).revision += 1
            memory_library.sync_sources(session, app.state.settings, tenant)
    monkeypatch.setattr(TencentClient, "call", original)
    with app.state.db.session(tenant) as session:
        memory_library.maintain(session, app.state.settings, tenant)
        _, inventory = memory_runtime.inventory(session, app.state.settings, tenant)
        assert len(inventory["raw"]) == 2  # current + protected historic, uncertain third version removed
    assert agent.get(f"/v2/library/objects/{page['id']}").status_code == 200
