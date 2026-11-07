import os
from uuid import uuid4

import pytest
from conftest import drain
from test_tencent_memory import item, publish
from test_tencent_memory import memory_app as memory_app


@pytest.mark.skipif(os.getenv("AKB_TEST_TENCENT") != "1", reason="requires real Tencent")
def test_reparse_invalidates_receipt_and_keeps_human_page(memory_app):
    app, browser, agent, worker, tenant = memory_app
    r = browser.post(
        "/v1/sources/upload",
        files={"file": ("t.csv", "编号,学时（学时）\nC1,36\n".encode(), "text/csv")},
        data={"subject_scope": "self"},
    )
    assert r.status_code == 202, r.text
    sid = r.json()["source"]["id"]
    drain(worker)
    source = agent.get(f"/v1/sources/{sid}").json()
    version = next(s["version"] for s in agent.get("/v2/library").json()["sources"] if s["id"] == sid)
    evs = agent.get(f"/v1/sources/{sid}/evidence").json()["items"]

    def anchor(ev):
        return {"evidence_id": ev["id"], "source_version": version, "quote": ev["text"]}

    field = {
        "key": "hours",
        "label": "学时",
        "operation": "sum",
        "value_type": "number",
        "unit": "学时",
        "rows": [
            {
                "entity_key": "C1",
                "value": "36",
                "unit": "学时",
                "evidence": anchor(evs[1]),
                "unit_evidence": anchor(evs[0]),
                "value_column": 2,
            }
        ],
    }
    plan = agent.post("/v2/filling/prepare", json={"request_id": str(uuid4()), "fields": [field]})
    assert plan.status_code == 200, plan.text
    assert plan.json()["values"]["hours"] == "36"
    protected = publish(
        browser,
        item("page", key="protected-reparse", content="人工备注必须保留", source_versions={sid: version}),
    )
    endpoint = f"/v2/library/sources/{sid}/reparse"
    rr = agent.post(endpoint, json={"expected_revision": source["revision"]})
    assert rr.status_code == 202, rr.text
    assert agent.post(endpoint, json={"expected_revision": source["revision"]}).status_code == 409
    assert (
        agent.post(
            "/v2/filling/" + plan.json()["receipt_id"] + "/validate",
            json={"artifact": "draft", "values": plan.json()["values"]},
        ).status_code
        == 409
    )
    drain(worker)
    updated = agent.get(f"/v1/sources/{sid}").json()
    assert updated["sha256"] == source["sha256"] and updated["revision"] == source["revision"] + 1
    assert agent.post("/v2/library/evidence/resolve", json=anchor(evs[0])).status_code in {404, 409}
    manual = agent.get("/v2/library/objects/" + protected["id"]).json()
    assert (
        manual["protected"] and not manual["current"] and manual["historical_content"] == "人工备注必须保留"
    )
