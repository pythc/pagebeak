from io import BytesIO

import pytest
from conftest import register
from docx import Document
from test_knowledge import accept, fact, source_task, submit, target


def add(client, worker, text, kind, entity, values, reject=False):
    task, evidence = source_task(client, worker, text)
    response, _ = submit(client, task, evidence, [
        fact(evidence[0], value, kind=kind, field=field, entity=entity) for field, value in values.items()
    ])
    assert response.status_code == 200, response.text
    ids = response.json()["fact_ids"]
    if reject:
        for fid in ids:
            r = client.post(f"/v1/knowledge/facts/{fid}/decisions", json={"action": "reject", "expected_revision": 1})
            assert r.status_code == 200
    else:
        accept(client, ids)
    return ids


def field(key, query, **overrides):
    return {"key": key, "label": key, "fact_key": key, "knowledge": query, "target": target(query), **overrides}


def prepare(client, fields):
    response = client.post("/v1/forms/drafts", json={"title": "回归验收", "fields": fields})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("reverse", [False, True])
def test_totals_ignore_rejected_and_definitely_excluded_entities(app, worker, reverse):
    _, client = app
    register(client)
    for entity, hours in [("spring", "28学时"), ("autumn", "36学时"), ("spring", "28学时")]:
        add(client, worker, f"李若晴2025年课程记录：已完成，{hours}。", "teaching", entity,
            {"hours": hours, "year": "2025", "status": "已完成"})
    add(client, worker, "李若晴2025年已取消课程，原计划99学时。", "teaching", "cancelled",
        {"hours": "99学时", "year": "2025", "status": "已取消"}, reject=True)
    add(client, worker, "李若晴2024年旧记录，20学时，完成状态未注明。", "teaching", "old",
        {"hours": "20学时", "year": "2024"})
    filters = {"year": "2025", "status": "已完成"}
    if reverse:
        filters = dict(reversed(list(filters.items())))
    answer = prepare(client, [field("hours", {"kind": "teaching", "field": "hours", "operation": "sum",
                                               "filters": filters})])["answers"][0]
    assert answer["status"] == "ready" and answer["value"] == "64"
    assert answer["calculation"]["record_count"] == 2
    assert len(answer["evidence_ids"]) == 3
    # Missing state on a potentially relevant record must still prevent a misleading total.
    add(client, worker, "李若晴2025年另有课程12学时，完成状态未注明。", "teaching", "unknown",
        {"hours": "12学时", "year": "2025"})
    assert prepare(client, [field("hours", {"kind": "teaching", "field": "hours", "operation": "sum",
                                            "filters": filters})])["answers"][0]["status"] != "ready"


def test_single_value_sum_and_target_units_use_exact_decimal(app, worker):
    _, client = app
    register(client)
    add(client, worker, "李若晴主持甲项目，金额7.50万元。", "project", "a", {"amount_yuan": "7.50万元"})
    add(client, worker, "李若晴主持乙项目，金额2,345.67元。", "project", "b", {"amount_yuan": "2,345.67元"})
    single = {"kind": "project", "field": "amount_yuan", "entity_key": "a"}
    total = {"kind": "project", "field": "amount_yuan", "operation": "sum"}
    fields = [field("single", single), field("total", total),
              field("wan", total, target={**target(total), "unit": "万元"})]
    answers = prepare(client, fields)["answers"]
    assert [a["value"] for a in answers] == ["75000", "77345.67", "7.734567"]
    assert answers[0]["conversion"]["raw_value"] == "7.50万元"
    assert all(a["conversion"]["rounded"] is False for a in answers)
    assert prepare(client, [field("integer", total, target={**target(total), "value_type": "integer"})])["answers"][0]["status"] == "blocked"


def test_destination_contract_blocks_wrong_operation_field_unit_and_missing_contract(app, worker):
    _, client = app
    register(client)
    add(client, worker, "李若晴主持已结题的甲项目，2025年立项金额7.5万元。", "project", "a",
        {"title": "甲项目", "role": "主持", "status": "已结题", "amount_yuan": "7.5万元"})
    title = {"kind": "project", "field": "title", "filters": {"role": "主持", "status": "已结题"}}
    count = {**title, "operation": "count"}
    money = {"kind": "project", "field": "amount_yuan", "entity_key": "a"}
    cases = [field("wrong_operation", count, target=target(title)),
             field("wrong_type", count, target={**target(count), "value_type": "text"}),
             field("wrong_unit", money, target={**target(money), "unit": "学时"}),
             field("missing_contract", title, target=None),
             field("wrong_filter", title, target=target({**title, "filters": {"role": "参与"}})),
             field("correct", title)]
    answers = prepare(client, cases)["answers"]
    assert [a["status"] for a in answers] == ["blocked"] * 5 + ["ready"]
    assert answers[-1]["value"] == "甲项目"


@pytest.mark.parametrize("operation,expected", [("value", "金额为8.24万元"), ("sum", "金额合计为8.24万元")])
def test_narrative_money_label_matches_converted_output_unit(app, worker, operation, expected):
    _, client = app
    register(client)
    add(client, worker, "李若晴甲项目批准额度为82,400元。", "project", "a", {"amount_yuan": "82,400元"})
    query = {"kind": "project", "field": "amount_yuan", "operation": operation, "entity_key": "a"}
    draft = prepare(client, [
        field("budget", query, target={**target(query), "unit": "万元"}),
        {"key": "story", "label": "事迹", "fact_key": "story", "narrative": {"field_keys": ["budget"]}},
    ])
    story = draft["answers"][-1]
    assert story["status"] == "ready" and expected in story["value"]
    assert "（元）" not in story["value"]
    assert story["sentences"][0]["fact_ids"] and story["sentences"][0]["evidence_ids"]


def test_narrative_and_saved_word_preserve_degree_meaning_and_reject_paraphrase(app, worker):
    application, client = app
    register(client)
    ids = add(client, worker, "李若晴于2018年6月取得博士学位，学位授予机构为北岭学院；毕业时间未记载。", "education", "degree",
              {"degree": "博士", "institution": "北岭学院"})
    fields = [field("degree", {"kind": "education", "field": "degree"}),
              field("school", {"kind": "education", "field": "institution"}, label="毕业院校"),
              {"key": "story", "label": "个人事迹", "fact_key": "story", "max_length": 2000,
               "narrative": {"field_keys": ["degree", "school"]}}]
    draft = prepare(client, fields)
    story = draft["answers"][-1]
    assert story["status"] == "ready" and "毕业" not in story["value"]
    assert "博士" in story["value"] and "北岭学院" in story["value"]
    assert len(story["sentences"]) == 2 and all(s["fact_ids"] and s["evidence_ids"] for s in story["sentences"])
    values = {a["key"]: a["value"] for a in draft["answers"]}
    path = f"/v1/forms/drafts/{draft['id']}/validate"
    assert client.post(path, json={"values": values}).json()["valid"]
    missing = client.post(path, json={"values": {k: v for k, v in values.items() if k != "degree"}}).json()
    assert missing["issues"] == [{"key": "degree", "code": "missing_field"}]
    extra = client.post(path, json={"values": {**values, "surprise": "新增"}}).json()
    assert extra["issues"] == [{"key": "surprise", "code": "unknown_field"}]
    bad = {**values, "story": "李若晴于2018年6月博士毕业。"}
    assert client.post(path, json={"values": bad}).json()["issues"] == [
        {"key": "story", "code": "changed_value", "message": "填写值与草稿不一致，禁止改写、改单位或补填未知"}]
    doc = Document()
    doc.add_paragraph("{{field:story}}")
    doc.add_paragraph("人工备注：保留此行。")
    buf = BytesIO()
    doc.save(buf)
    response = client.post(f"/v1/forms/drafts/{draft['id']}/document", files={"file": ("申报.docx", buf.getvalue())})
    assert response.status_code == 200
    filled = Document(BytesIO(response.content))
    assert filled.paragraphs[0].text == story["value"]
    assert filled.paragraphs[1].text == "人工备注：保留此行。"
    client.post(f"/v1/knowledge/facts/{ids[0]}/decisions", json={"action": "correct", "expected_revision": 2, "value": "硕士"})
    assert client.post(path, json={"values": values}).json()["issues"][0]["code"] == "stale"
    # Another tenant cannot validate or learn whether this draft exists.
    from fastapi.testclient import TestClient
    with TestClient(application) as other:
        register(other)
        assert other.post(path, json={"values": values}).status_code == 404


def test_narrative_missing_sensitive_and_invalid_references_do_not_leak(app, worker):
    _, client = app
    register(client)
    add(client, worker, "李若晴的邮箱为li@example.test。", "profile", "profile", {"email": "li@example.test"})
    fields = [field("email", {"kind": "profile", "field": "email"}),
              {"key": "story", "label": "介绍", "fact_key": "story", "narrative": {"field_keys": ["email"]}}]
    answers = prepare(client, fields)["answers"]
    assert all(a["status"] == "blocked" and a["value"] is None for a in answers)
    fields[-1]["narrative"]["field_keys"] = ["story"]
    assert client.post("/v1/forms/drafts", json={"title": "坏引用", "fields": fields}).status_code == 422


def test_legacy_draft_is_invalidated_on_upgrade_without_source_change():
    from types import SimpleNamespace

    from akb.services import draft_dict

    draft = SimpleNamespace(id="old", title="旧草稿", snapshot_revision=12,
                            answers=[{"key": "money", "status": "ready", "value": "7万元"}], fields=[])
    session = SimpleNamespace(get=lambda *_: SimpleNamespace(revision=12))
    old = draft_dict(session, "tenant", draft)
    assert old["stale"] and not old["answers"]
    draft.answers[0]["contract_version"] = "form-evidence-v2"
    assert not draft_dict(session, "tenant", draft)["stale"]


@pytest.mark.parametrize("raw", ["1,23元", "12,34,567元", "1,,000元", "NaN元", "2e5元"])
def test_malformed_amount_is_never_silently_normalized(raw):
    from akb.knowledge import number

    with pytest.raises(ValueError):
        number(raw, "amount_yuan")
