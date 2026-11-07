"""Real stdio file upload into an isolated synthetic account; not a WorkBuddy model run."""

import asyncio
import hashlib
import json
import secrets
import shutil
from pathlib import Path
from uuid import uuid4

import httpx
from akb.config import ROOT, Settings
from docx import Document
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openpyxl import Workbook
from pptx import Presentation
from reportlab.pdfgen import canvas


def fixtures(directory):
    directory.mkdir(mode=0o700)
    docx = directory / "synthetic-resume.docx"
    doc = Document()
    doc.add_heading("Synthetic project record", 0)
    doc.add_paragraph("MCPUPLOADDOCXSYNTH: the synthetic teacher leads a document management project.")
    doc.save(docx)
    xlsx = directory / "synthetic-workbook.xlsx"
    workbook = Workbook()
    workbook.active.append(["Project", "Evidence"])
    workbook.active.append(["Synthetic classroom", "MCPUPLOADXLSXSYNTH"])
    workbook.save(xlsx)
    pptx = directory / "synthetic-project.pptx"
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "Synthetic project review"
    slide.placeholders[1].text = "MCPUPLOADPPTXSYNTH: original slide material."
    deck.save(pptx)
    pdf = directory / "synthetic-report.pdf"
    page = canvas.Canvas(str(pdf))
    page.drawString(72, 740, "MCPUPLOADPDFSYNTH: original synthetic report.")
    page.save()
    large = directory / "synthetic-multichunk.txt"
    with large.open("wb") as stream:
        stream.write(b"MCPUPLOADMULTICHUNKSYNTH: original bytes across several chunks.\n")
        for _ in range(9):
            stream.write(b" " * 1024**2)
    return [(docx, "MCPUPLOADDOCXSYNTH"), (xlsx, "MCPUPLOADXLSXSYNTH"),
            (pptx, "MCPUPLOADPPTXSYNTH"), (pdf, "MCPUPLOADPDFSYNTH"),
            (large, "MCPUPLOADMULTICHUNKSYNTH")]


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


async def main():
    settings = Settings()
    evidence = ROOT / "work/mcp-file-upload-acceptance" / uuid4().hex
    evidence.mkdir(parents=True, mode=0o700)
    samples = fixtures(evidence / "fixtures")
    report = {"test_kind": "Real stdio MCP into an isolated synthetic account, not WorkBuddy model acceptance",
              "files": [], "large_2_gib_transfer": "NOT_RUN"}
    source_ids = []
    with httpx.Client(base_url=f"http://127.0.0.1:{settings.api_port}", trust_env=False, timeout=40) as browser:
        signed = browser.post("/v1/auth/register", json={
            "email": f"mcp-upload-{uuid4().hex}@example.test", "password": secrets.token_urlsafe(32),
            "display_name": "MCP 原件上传合成验收"})
        signed.raise_for_status()
        browser.headers["X-CSRF-Token"] = signed.json()["csrf"]
        try:
            kb = browser.get("/v1/context").json()["knowledge_base_id"]
            report["knowledge_base_id"] = kb
            issued = browser.post("/v1/tokens", json={
                "label": "synthetic MCP upload", "scopes": ["knowledge:read", "knowledge:write", "profile:read"]})
            issued.raise_for_status()
            server = StdioServerParameters(command=str(ROOT / ".venv/bin/python"),
                args=["-m", "akb.mcp_server"], cwd=str(ROOT), env={
                    "AKB_API_URL": f"http://127.0.0.1:{settings.api_port}", "AKB_TOKEN": issued.json()["token"],
                    "AKB_EXPECTED_KB_ID": kb, "AKB_FILE_INBOX_ROOT": str(evidence / "local-inbox")})
            with (evidence / "mcp-stderr.log").open("w") as errors:
                async with stdio_client(server, errlog=errors) as (read, write), ClientSession(read, write) as session:
                    await session.initialize()
                    available = await session.list_tools()
                    names = {tool.name for tool in available.tools}
                    assert len(names) == (35 if settings.knowledge_engine == "tencent" else 38)
                    assert {"prepare_file_upload", "upload_file", "read_file_upload", "cancel_file_upload"} <= names
                    report["mcp_tools"] = len(names)

                    async def invoke(tool, arguments=None):
                        result = await session.call_tool(tool, arguments or {})
                        assert not result.isError, f"{tool}: {str(result.content)[:500]}"
                        return json.loads(result.content[0].text)

                    await invoke("get_knowledge_context", {"expected_knowledge_base_id": kb})
                    prepared = await invoke("prepare_file_upload")
                    assert prepared["upload_limit_bytes"] == 2 * 1024**3
                    inbox = Path(prepared["staging_directory"])
                    assert inbox.stat().st_mode & 0o777 == 0o700
                    for original, marker in samples:
                        staged = inbox / original.name
                        shutil.copyfile(original, staged)
                        key = uuid4().hex
                        result = await invoke("upload_file", {
                            "local_path": str(staged), "request_id": key, "filename": original.name})
                        source_ids.append(result["source"]["id"])
                        assert result["sha256_verified"] and result["source"]["sha256"] == file_hash(original)
                        assert result["source"]["subject_scope"] == "reference"
                        digest = hashlib.sha256()
                        with browser.stream("GET", f"/v1/sources/{source_ids[-1]}/original") as response:
                            response.raise_for_status()
                            for data in response.iter_bytes(1024 * 1024):
                                digest.update(data)
                        assert digest.hexdigest() == file_hash(original)
                        replay = await invoke("upload_file", {"local_path": str(staged), "request_id": key})
                        assert replay["source"]["id"] == source_ids[-1] and replay["replayed"]
                        for _ in range(80):
                            job = await invoke("get_job", {"job_id": result["job_id"]})
                            if job["status"] in {"completed", "failed", "cancelled"}:
                                break
                            await asyncio.sleep(0.25)
                        assert job["status"] == "completed", job
                        found = await invoke("search_knowledge", {"query": marker})
                        assert source_ids[-1] in str(found), (original.name, found)
                        catalog = await invoke("read_catalog")
                        assert source_ids[-1] in str(catalog)
                        status = await invoke("read_file_upload", {"upload_id": result["upload_id"]})
                        assert status["offset"] == original.stat().st_size and status["status"] == "completed"
                        # Cancellation of a completed upload must not remove its original.
                        cancelled = await invoke("cancel_file_upload", {"upload_id": result["upload_id"]})
                        assert cancelled["status"] == "completed"
                        report["files"].append({"filename": original.name, "bytes": original.stat().st_size,
                            "original_sha256_match": True, "retry_same_source": True, "processing": job["status"],
                            "search_and_catalog": "PASS", "source_id": source_ids[-1]})
                        print(json.dumps({"file": original.name, "result": "PASS"}), flush=True)
                    profile = await invoke("get_profile")
                    assert not profile["candidates"]
                    report["personal_facts_auto_confirmed"] = False
                    report["result"] = "PASS"
        finally:
            # Remove only sources made by this disposable synthetic account.
            for source_id in source_ids:
                browser.delete("/v1/sources/" + source_id).raise_for_status()
            for credential in browser.get("/v1/tokens").json():
                if not credential["revoked"]:
                    browser.delete("/v1/tokens/" + credential["id"]).raise_for_status()
            browser.post("/v1/auth/logout").raise_for_status()
            report["synthetic_credentials_revoked"] = True
            (evidence / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"report": str(evidence / "verification.json"), "result": report.get("result", "FAILED")}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
