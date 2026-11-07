"""Independent saved-artifact checks for the fresh synthetic WorkBuddy workflow."""

import argparse
import json

import httpx
from akb.config import ROOT
from docx import Document

LABELS = {
    "name": "姓名",
    "title": "当前职称",
    "degree": "学位",
    "amount": "2025年主持且已结题项目批准经费（万元）",
    "graduation": "毕业日期",
    "phone": "联系电话",
    "narrative": "个人事迹",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    args = parser.parse_args()
    folder = (ROOT / args.folder).resolve()
    assert folder.is_relative_to(ROOT / "work/local-documents-acceptance-20261026")
    auth = json.loads((folder / "private/model-session.json").read_text())
    initial = json.loads((folder / "private/phase-one-model.json").read_text())
    final = json.loads((folder / "agent-space/模型结果-增量后.json").read_text())
    expected = {
        "name": "沈知禾",
        "title": "教授",
        "degree": "工程硕士",
        "amount": "12.5",
        "graduation": "",
        "phone": "",
    }
    assert all(final["values"][k] == v for k, v in expected.items())
    web = json.loads((folder / "web-model-readback.json").read_text())
    doc = Document(folder / "agent-space/模型填写结果-增量后.docx")
    rows = {row.cells[0].text: row.cells[1].text for row in doc.tables[0].rows[1:]}
    word = {key: rows[label] for key, label in LABELS.items()}
    assert web == word == final["values"]
    report = {
        "result": "PASS",
        "kind": "genuine WorkBuddy run; synthetic fixtures and independent artifact scoring, not a blinded benchmark",
        "initial_values": initial["values"],
        "final_values": final["values"],
        "validations": {},
    }
    with httpx.Client(
        base_url="http://127.0.0.1:5876",
        trust_env=False,
        timeout=35,
        headers={
            "Authorization": "Bearer " + auth["token"],
            "X-AKB-Knowledge-Base": auth["knowledge_base_id"],
        },
    ) as api:
        old = api.post(
            "/v2/filling/" + initial["receipt_id"] + "/validate",
            json={"values": initial["values"], "artifact": "word_readback"},
        )
        assert old.status_code == 409
        report["validations"]["old_receipt_rejected"] = True
        for kind, values in (("web_readback", web), ("word_readback", word)):
            r = api.post(
                "/v2/filling/" + final["receipt_id"] + "/validate", json={"values": values, "artifact": kind}
            )
            r.raise_for_status()
            assert r.json()["valid"]
            report["validations"][kind] = r.json()
        manual = json.loads((folder / "private/manual-page.json").read_text())
        r = api.get("/v2/library/objects/" + manual["id"])
        r.raise_for_status()
        page = r.json()
        assert page["protected"] and not page["current"] and manual["note"] in page["historical_content"]
        assert page["revision"] == manual["revision"]
        report["validations"]["human_page_preserved_and_stale"] = True
        library = api.get("/v2/library").json()
        current = []
        for entry in library["items"]:
            if entry["kind"] == "page" and entry["current"]:
                body = api.get("/v2/library/objects/" + entry["id"]).json()
                current.append(body)
        assert any("聘为教授" in item["content"] for item in current)
        assert any("HL25-F" in item["content"] for item in current)
        report["current_pages"] = [
            {"id": i["id"], "title": i["title"], "citations": len(i["citations"])} for i in current
        ]
        report["local_catalog_total"] = library["local_catalog"]["total"]
        (folder / "final-catalog.json").write_text(json.dumps(library, ensure_ascii=False, indent=2))
    (folder / "model-score.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "result": "PASS",
                "checks": list(report["validations"]),
                "current_pages": len(report["current_pages"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
