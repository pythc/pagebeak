from copy import deepcopy
from uuid import uuid4

import pytest
from akb import memory_filling as f
from akb.parsing import parse_document as parse
from fastapi import HTTPException


def fixtures(monkeypatch, unit="学时", value="36"):
    header_id, row_id = str(uuid4()), str(uuid4())
    version = "a" * 64

    def anchor(eid, quote):
        return {"evidence_id": eid, "source_version": version, "quote": quote}

    anchors = {
        header_id: {
            "source_id": "one",
            "source_version": version,
            "quote": "编号 | 学时（学时） | 人数（人次）",
            "locator": {"kind": "csv_row", "row": 1, "cells": ["编号", "学时（学时）", "人数（人次）"]},
        },
        row_id: {
            "source_id": "one",
            "source_version": version,
            "quote": "C1 | 36 | 41",
            "locator": {"kind": "csv_row", "row": 2, "cells": ["C1", "36", "41"]},
        },
    }
    monkeypatch.setattr(f, "resolve", lambda session, a: anchors[str(a.evidence_id)])
    field = {
        "key": "hours",
        "label": "学时",
        "operation": "sum",
        "value_type": "number",
        "unit": unit,
        "rows": [
            {
                "entity_key": "C1",
                "value": value,
                "unit": unit,
                "evidence": anchor(row_id, "C1 | 36 | 41"),
                "unit_evidence": anchor(header_id, anchors[header_id]["quote"]),
                "value_column": 2,
            }
        ],
    }
    return field, anchors, header_id, row_id


def run(field):
    return f.compute(None, f.FillingPlan(request_id=uuid4(), fields=[field]))


def test_column_header_units_and_duplicate_rows(monkeypatch):
    field, anchors, _, _ = fixtures(monkeypatch)
    field["rows"] *= 2
    values, details, versions = run(field)
    assert values == {"hours": "36"} and details["hours"]["duplicates"] == ["C1"]
    assert len(details["hours"]["evidence"]) == 4 and versions == {"one": "a" * 64}


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_column",
        "cross_source",
        "cross_version",
        "cross_table",
        "header_after",
        "missing_cells",
        "wrong_cell_value",
        "wrong_unit",
    ],
)
def test_reject_header_mismatch(monkeypatch, mutation):
    field, a, h, r = fixtures(monkeypatch)
    if mutation == "wrong_column":
        field["rows"][0]["value_column"] = 3
    if mutation == "cross_source":
        a[h]["source_id"] = "other"
    if mutation == "cross_version":
        a[h]["source_version"] = "b" * 64
    if mutation == "cross_table":
        a[h]["locator"]["table"] = 2
    if mutation == "header_after":
        a[h]["locator"]["row"] = 3
    if mutation == "missing_cells":
        a[r]["locator"].pop("cells")
    if mutation == "wrong_cell_value":
        a[r]["locator"]["cells"][1] = "360"
    if mutation == "wrong_unit":
        field["rows"][0]["unit"] = "人次"
    with pytest.raises(HTTPException) as err:
        run(field)
    assert err.value.status_code == 422
    assert err.value.detail["errors"][0]["field_key"] == "hours"


@pytest.mark.parametrize(
    "quote,value,unit,expected",
    [
        ("P1 批准1.275万元，到账8元", "1.275", "元", None),
        ("P1 批准1.275万元，到账8元", "1.275", "万元", "12750"),
        ("P1 万元：1.275", "1.275", "元", None),
        ("P1 元：1.275", "1.275", "元", "1.275"),
        ("P1 批准93,450.75 | 元", "93,450.75", "元", "93450.75"),
    ],
)
def test_amount_unit_is_bound_to_number(monkeypatch, quote, value, unit, expected):
    eid = str(uuid4())
    monkeypatch.setattr(
        f,
        "resolve",
        lambda *_: {
            "source_id": "one",
            "source_version": "a" * 64,
            "quote": quote,
            "locator": {"kind": "line", "line": 1},
        },
    )
    field = {
        "key": "amount",
        "label": "批准额",
        "operation": "sum",
        "value_type": "number",
        "unit": "元",
        "rows": [
            {
                "entity_key": "P1",
                "value": value,
                "unit": unit,
                "evidence": {"evidence_id": eid, "source_version": "a" * 64, "quote": quote},
            }
        ],
    }
    if expected is None:
        with pytest.raises(HTTPException):
            run(field)
    else:
        assert run(field)[0]["amount"] == expected


def test_dimensionless_rank_and_batched_errors(monkeypatch):
    field, a, h, r = fixtures(monkeypatch)
    rank = deepcopy(field)
    rank.update(key="rank", label="排名", operation="extract", value_type="integer", unit="")
    rank["rows"][0].update(unit="", unit_evidence=None, value_column=None)
    assert run(rank)[0]["rank"] == "36"
    bad = deepcopy(field)
    bad["rows"][0]["value_column"] = 3
    other = deepcopy(bad)
    other["key"] = "students"
    with pytest.raises(HTTPException) as err:
        f.compute(None, f.FillingPlan(request_id=uuid4(), fields=[bad, other]))
    assert [e["field_key"] for e in err.value.detail["errors"]] == ["hours", "students"]


def test_csv_structural_cells_preserve_embedded_separator():
    data = '编号,说明,学时（学时）\nC1,"含 | 分隔符",36\n'.encode()
    result = parse("test.csv", data)
    assert result.blocks[1].locator["cells"] == ["C1", "含 | 分隔符", "36"]
    long = parse("long.csv", ("编号,说明\nC1," + "x" * 5000).encode())
    assert all("cells" not in b.locator for b in long.blocks if b.locator["row"] == 2)


def test_office_table_columns_keep_blanks():
    from io import BytesIO

    from docx import Document
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["编号", None, "学时（学时）"])
    ws.append(["C1", None, 36])
    buf = BytesIO()
    wb.save(buf)
    blocks = parse("test.xlsx", buf.getvalue()).blocks
    assert blocks[0].locator["cells"] == ["编号", "", "学时（学时）"]
    assert blocks[1].locator["row"] == 2 and blocks[1].locator["cells"] == ["C1", "", "36"]
    doc = Document()
    table = doc.add_table(rows=2, cols=3)
    for cells, vals in zip(table.rows, [["编号", "", "学时（学时）"], ["C1", "", "36"]], strict=True):
        for cell, val in zip(cells.cells, vals, strict=True):
            cell.text = val
    buf = BytesIO()
    doc.save(buf)
    blocks = parse("test.docx", buf.getvalue()).blocks
    assert blocks[1].locator["cells"] == ["C1", "", "36"] and blocks[1].locator["table"] == 1


def test_explicit_header_unit_overrides_label_and_rejects_ambiguous_units():
    assert f.unit_in_header("人次", "人数（人次）")
    assert not f.unit_in_header("人", "人数（人次）")
    assert not f.unit_in_header("元", "批准经费（万元）")
    assert not f.unit_in_header("元", "金额（元/万元）")
