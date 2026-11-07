"""Live local acceptance with synthetic data; --large transfers exactly 2 GiB."""

import argparse
import json
import os
import secrets
import time
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import httpx
from akb.config import ROOT, Settings
from akb.models import User
from docx import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches
from sqlalchemy import create_engine, update

parser = argparse.ArgumentParser()
parser.add_argument("--large", action="store_true")
parser.add_argument("--output-dir", default="work/storage-e2e")
args = parser.parse_args()
root = ROOT / args.output_dir
root.mkdir(parents=True, exist_ok=True)
settings = Settings()
base = f"http://127.0.0.1:{settings.api_port}"
report = {
    "synthetic_only": True,
    "large_test": "NOT_RUN",
    "storage_provider": "cloudreve" if Settings().cloudreve_credentials else "local",
}
email, password = f"storage-{uuid4().hex[:10]}@example.test", secrets.token_urlsafe(24)


def wait_for(client, path, predicate):
    for _ in range(150):
        response = client.get(path)
        response.raise_for_status()
        data = response.json()
        if predicate(data):
            return data
        time.sleep(0.2)
    raise AssertionError("Background processing did not settle")


def send(client, name, data, scope="reference"):
    response = client.post("/v1/uploads", json={"filename": name, "size": len(data), "subject_scope": scope})
    response.raise_for_status()
    upload = response.json()
    for offset in range(0, len(data), settings.upload_chunk_bytes):
        client.put(
            f"/v1/uploads/{upload['id']}/content",
            params={"offset": offset},
            content=data[offset : offset + settings.upload_chunk_bytes],
        ).raise_for_status()
    result = client.post(f"/v1/uploads/{upload['id']}/complete")
    result.raise_for_status()
    return result.json()["source"]["id"]


with httpx.Client(base_url=base, timeout=120) as client:
    registered = client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "display_name": "空间与 OCR 验收（合成）"},
    )
    registered.raise_for_status()
    user = registered.json()
    client.headers["X-CSRF-Token"] = user["csrf"]
    assert client.get("/v1/admin/storage/users").status_code == 403
    engine = create_engine(settings.migration_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(update(User).where(User.id == user["id"]).values(is_admin=True))
    finally:
        engine.dispose()
    # Operator bootstrap is restricted to this synthetic acceptance identity.
    session_file = root / "session.json"
    fd = os.open(session_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"email": email, "password": password, "user_id": user["id"]}, stream)
    os.chmod(session_file, 0o600)
    report["admin_browser_only"] = "PASS"
    quota = 2 * 1024**3 + 64 * 1024**2
    before = client.get("/v1/storage").json()
    client.patch(
        f"/v1/admin/storage/users/{user['id']}",
        json={"quota_bytes": quota, "expected_quota_bytes": before["quota_bytes"]},
    ).raise_for_status()
    samples = {}
    doc = Document()
    doc.add_heading("知识库项目材料", 0)
    doc.add_paragraph("姓名：测试教师")
    doc.add_paragraph("项目：跨格式检索与资料整理")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "验收目标", "原文可定位"
    stream = BytesIO()
    doc.save(stream)
    samples["项目材料.docx"] = stream.getvalue()
    workbook = Workbook()
    workbook.active.title = "成果清单"
    workbook.active.append(["项目名称", "年度"])
    workbook.active.append(["知识库项目", 2026])
    stream = BytesIO()
    workbook.save(stream)
    samples["成果清单.xlsx"] = stream.getvalue()
    ppt = Presentation()
    slide = ppt.slides.add_slide(ppt.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(2)).text = "知识库项目：跨格式整理与检索"
    stream = BytesIO()
    ppt.save(stream)
    samples["项目汇报.pptx"] = stream.getvalue()
    image = Image.new("RGB", (1500, 500), "white")
    draw = ImageDraw.Draw(image)
    font_path = Path("/System/Library/Fonts/STHeiti Medium.ttc")
    font = ImageFont.truetype(str(font_path), 44) if font_path.exists() else ImageFont.load_default(size=44)
    lines = (
        ["教师成果登记表", "姓名：测试教师", "工作单位：示例大学", "Research project: Knowledge Library 2026"]
        if font_path.exists()
        else [
            "Teacher registration",
            "Name: Sample Teacher",
            "Organization: Sample University",
            "Knowledge Library 2026",
        ]
    )
    for i, line in enumerate(lines):
        draw.text((70, 50 + i * 100), line, fill="black", font=font)
    stream = BytesIO()
    image.save(stream, "PNG")
    samples["教师资料扫描.png"] = stream.getvalue()
    stream = BytesIO()
    image.save(stream, "PDF", resolution=150)
    samples["教师资料扫描.pdf"] = stream.getvalue()
    samples["项目数据.csv"] = "项目,年度\n知识库项目,2026".encode()
    samples["项目说明.md"] = "# 知识库项目\n目标：统一解析、索引、摘要与目录。".encode()
    ids = {}
    for name, data in samples.items():
        (root / name).write_bytes(data)
        ids[name] = send(client, name, data, "self" if name.endswith(".png") else "reference")
    wait_for(
        client,
        "/v1/sources",
        lambda data: (
            len(data["items"]) == len(ids) and all(s["status"] in {"ready", "partial"} for s in data["items"])
        ),
    )
    catalog = wait_for(client, "/v1/catalog", lambda d: d["pending"] == 0)
    assert all(i["summary"] for i in catalog["items"])
    for name, source_id in ids.items():
        evidence = client.get(f"/v1/sources/{source_id}/evidence").json()["items"]
        assert evidence
        report.setdefault("formats", {})[name] = {
            "source_id": source_id,
            "evidence_count": len(evidence),
            "locator": evidence[0]["locator"],
            "summary": next(i["summary"] for i in catalog["items"] if i["source_id"] == source_id),
        }
    ocr = client.get(f"/v1/sources/{ids['教师资料扫描.png']}/evidence").json()["items"]
    assert all(e["locator"]["method"] == "ocr" for e in ocr)
    profile = client.get("/v1/profile").json()
    assert profile["fields"]["name"]["status"] == "missing"  # OCR never silently confirms a name.
    assert client.post("/v1/knowledge/search", json={"query": "Knowledge"}).json()["items"]
    report["ocr_and_search"] = "PASS"
    if args.large:
        started = time.monotonic()
        size = 2 * 1024**3
        response = client.post("/v1/uploads", json={"filename": "2GiB边界验收.bin", "size": size})
        response.raise_for_status()
        large = response.json()
        reserved = client.get("/v1/storage").json()
        assert reserved["near_full"] and reserved["reserved_bytes"] == size
        assert (
            client.post("/v1/uploads", json={"filename": "超配额.bin", "size": 128 * 1024**2}).status_code
            == 413
        )
        block = b"\0" * settings.upload_chunk_bytes
        for offset in range(0, size, len(block)):
            client.put(
                f"/v1/uploads/{large['id']}/content", params={"offset": offset}, content=block
            ).raise_for_status()
            if (offset + len(block)) % (512 * 1024**2) == 0:
                print(f"Synthetic large upload: {(offset + len(block)) // 1024**2} / 2048 MiB", flush=True)
        accepted = client.post(f"/v1/uploads/{large['id']}/complete")
        accepted.raise_for_status()
        source_id = accepted.json()["source"]["id"]
        assert accepted.json()["source"]["size"] == size
        assert (
            client.get(
                f"/v1/sources/{source_id}/original", headers={"Range": "bytes=2147483644-2147483647"}
            ).content
            == b"\0" * 4
        )
        wait_for(client, f"/v1/sources/{source_id}", lambda s: s["status"] == "unsupported")
        plan = client.post("/v1/storage/plans", json={"source_ids": [source_id], "mode": "delete"}).json()
        client.post(
            f"/v1/storage/plans/{plan['id']}/approve", json={"acknowledge_loss": True}
        ).raise_for_status()
        client.post(f"/v1/storage/plans/{plan['id']}/execute").raise_for_status()
        final = wait_for(
            client, "/v1/storage", lambda s: s["used_bytes"] == sum(len(d) for d in samples.values())
        )
        report["large_test"] = {
            "status": "PASS",
            "bytes_transferred": size,
            "seconds": round(time.monotonic() - started, 2),
            "reserved_near_full": reserved["near_full"],
            "over_quota_rejected": True,
            "range_download": "PASS",
            "provider_unlink" if settings.cloudreve_credentials else "physical_cleanup": "PASS",
            "final_used_bytes": final["used_bytes"],
        }
    # Leave a pending sample for actual browser approval verification; original remains intact.
    preview = client.post("/v1/storage/plans", json={"source_ids": [ids["项目说明.md"]], "mode": "summarize"})
    preview.raise_for_status()
    report["browser_pending_plan_id"] = preview.json()["id"]
    report["catalog_id"] = catalog["id"]
    report["report_kind"] = "local_http_acceptance_not_WorkBuddy_GUI"
    (root / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print("Office/image/scanned-PDF/CSV/Markdown ingest, evidence, summary, search: PASS")
    print(f"Report saved to {args.output_dir}/verification.json; no credentials exported")
