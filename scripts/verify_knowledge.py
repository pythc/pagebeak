"""Local synthetic API + real stdio MCP acceptance. Fixture submissions are not a model accuracy test."""

import asyncio
import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from akb.config import ROOT
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BASE = "http://127.0.0.1:5876"
OUT = Path(os.environ.get("AKB_ACCEPTANCE_DIR", ROOT / "work/knowledge-layer-e2e"))


def save(name, data):
    (OUT / "results" / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


async def main():
    if (OUT / "private/session.json").exists():
        raise SystemExit("已有验收账号，拒绝覆盖凭据；请设置 AKB_ACCEPTANCE_DIR 为新的实验目录")
    fixtures = ROOT / "integrations/workbuddy/demo/knowledge-layer-fixtures"
    shutil.copytree(fixtures, OUT, dirs_exist_ok=True)
    (OUT / "results").mkdir(exist_ok=True)
    report = {"mode": "deterministic_fixture_over_real_mcp", "workbuddy_model": "NOT_RUN", "checks": {}}
    with httpx.Client(base_url=BASE, timeout=30) as browser:
        account = {
            "email": f"knowledge-{uuid4().hex[:10]}@example.test",
            "password": secrets.token_urlsafe(24),
            "display_name": "李若晴 · 知识层合成验收",
        }
        response = browser.post("/v1/auth/register", json=account)
        response.raise_for_status()
        browser.headers["X-CSRF-Token"] = response.json()["csrf"]
        context = browser.get("/v1/context").json()
        token = browser.post(
            "/v1/tokens",
            json={
                "label": "知识层合成验收",
                "scopes": ["knowledge:read", "knowledge:write", "profile:read", "forms:write"],
            },
        ).json()
        private = {
            **account,
            "token": token["token"],
            "token_id": token["id"],
            "knowledge_base_id": context["knowledge_base_id"],
        }
        session_file = OUT / "private/session.json"
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.parent.chmod(0o700)
        with os.fdopen(os.open(session_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
            json.dump(private, stream)
        save("context.json", context)

        def upload(path):
            result = browser.post(
                "/v1/sources/upload",
                files={"file": (path.name, path.read_bytes())},
                data={"subject_scope": "self"},
            )
            result.raise_for_status()
            return result.json()["source"]["id"]

        for path in sorted((OUT / "materials").glob("0[1-4]*")):
            upload(path)

        async def wait_tasks(count):
            for _ in range(150):
                result = browser.get("/v1/knowledge/tasks").json()
                if result["pending"] == count:
                    return result
                await asyncio.sleep(0.2)
            raise AssertionError("Background organization did not settle")

        await wait_tasks(4)
        receipts = []
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "akb.mcp_server"],
            env={
                "AKB_API_URL": BASE,
                "AKB_TOKEN": private["token"],
                "AKB_EXPECTED_KB_ID": context["knowledge_base_id"],
            },
        )
        async with stdio_client(server) as (read, write), ClientSession(read, write) as mcp:
            await mcp.initialize()
            names = [t.name for t in (await mcp.list_tools()).tools]
            assert len(names) == 38

            async def invoke(name, args=None, error=False):
                result = await mcp.call_tool(name, args or {})
                receipts.append({"tool": name, "is_error": bool(result.isError)})
                if error:
                    assert result.isError
                    return
                assert not result.isError, result.content
                return json.loads(result.content[0].text)

            await invoke(
                "get_knowledge_context", {"expected_knowledge_base_id": context["knowledge_base_id"]}
            )
            await invoke("get_knowledge_context", {"expected_knowledge_base_id": str(uuid4())}, error=True)
            report["checks"]["wrong_library_stops"] = True

            async def extract_pending():
                pending = await invoke("list_knowledge_tasks")
                published = []
                for task in pending["items"]:
                    lease = await invoke("claim_knowledge_task", {"task_id": task["id"]})
                    blocks = []
                    for offset in range(0, 1000, 30):
                        page = await invoke(
                            "read_source", {"source_id": task["source_id"], "offset": offset, "limit": 30}
                        )
                        blocks.extend(page["items"])
                        if len(page["items"]) < 30:
                            break
                    items = []

                    def propose(
                        e,
                        field,
                        value,
                        kind="employment",
                        entity="current",
                        subject="李若晴",
                        scope="self",
                        target=items,
                        **extra,
                    ):
                        target.append(
                            {
                                "kind": kind,
                                "subject": subject,
                                "subject_scope": scope,
                                "entity_key": entity,
                                "field": field,
                                "value": value,
                                "quote": e["text"],
                                "evidence_id": e["id"],
                                **extra,
                            }
                        )

                    for e in blocks:
                        text = e["text"]
                        if "聘任为" in text:
                            new = "2026年10月1日" in text
                            propose(e, "name", "李若晴", kind="profile")
                            propose(
                                e,
                                "job_title",
                                "教授" if new else "副教授",
                                valid_from="2026-10-01" if new else "2024-09-01",
                                time_quote=text,
                            )
                            propose(e, "organization", "澄川学院")
                        if "不是系主任" in text:
                            propose(e, "administrative_position", "系主任", polarity="negative")
                        if "li.ruoqing@" in text:
                            propose(e, "email", "li.ruoqing@chengchuan.example.test", kind="profile")
                        for course, hours, subject in [
                            ("T25-A", "48学时", "李若晴"),
                            ("T25-B", "32学时", "李若晴"),
                            ("T25-C", "96学时", "周云"),
                        ]:
                            if course in text:
                                for field, value in [("title", course), ("hours", hours), ("year", "2025")]:
                                    propose(
                                        e,
                                        field,
                                        value,
                                        kind="teaching",
                                        entity=course,
                                        subject=subject,
                                        scope="self" if subject == "李若晴" else "reference",
                                    )
                    payload = {
                        "expected_revision": lease["revision"],
                        "request_id": str(uuid4()),
                        "items": items,
                        "complete": True,
                        "reviewed_evidence_ids": [e["id"] for e in blocks],
                    }
                    r = await invoke("submit_knowledge_task", {"task_id": task["id"], "submission": payload})
                    replay = await invoke(
                        "submit_knowledge_task", {"task_id": task["id"], "submission": payload}
                    )
                    assert r == replay
                    published.extend(r["fact_ids"])
                return published

            ids = await extract_pending()
            candidate_view = await invoke("read_knowledge")
            assert candidate_view == browser.get("/v1/knowledge").json()
            save("candidates.json", candidate_view)
            fields = [
                {"key": k, "label": k, "fact_key": k}
                for k in ["name", "organization", "job_title", "administrative_position"]
            ]
            fields.append(
                {
                    "key": "teaching_hours",
                    "label": "2025 年教学学时",
                    "fact_key": "teaching_hours",
                    "knowledge": {
                        "kind": "teaching",
                        "field": "hours",
                        "operation": "sum",
                        "filters": {"year": "2025"},
                    },
                }
            )
            fields[-1]["target"] = {"knowledge": fields[-1]["knowledge"], "value_type": "number", "unit": "学时"}
            pending_draft = await invoke("prepare_form", {"title": "教师资料核对", "fields": fields})
            assert all(a["value"] is None for a in pending_draft["answers"])
            report["checks"]["candidates_do_not_fill"] = True
            for fid in ids:
                r = browser.post(
                    f"/v1/knowledge/facts/{fid}/decisions", json={"action": "accept", "expected_revision": 1}
                )
                r.raise_for_status()
            org = next(
                f for f in browser.get("/v1/knowledge").json()["items"] if f["field"] == "organization"
            )
            browser.post(
                f"/v1/knowledge/facts/{org['id']}/decisions",
                json={"action": "correct", "expected_revision": org["revision"], "value": "澄川大学"},
            ).raise_for_status()
            catalog = browser.get("/v1/catalog").json()
            entry = catalog["items"][0]
            browser.patch(
                f"/v1/catalog/entries/{entry['id']}",
                json={
                    "expected_revision": catalog["revision"],
                    "title": "聘任材料 · 人工命名",
                    "note": "保留这条人工备注",
                },
            ).raise_for_status()
            before = await invoke("prepare_form", {"title": "教师资料核对", "fields": fields})
            save("draft-before-increment.json", before)
            values = {a["key"]: a["value"] for a in before["answers"]}
            assert values == {
                "name": "李若晴",
                "organization": "澄川大学",
                "job_title": "副教授",
                "administrative_position": None,
                "teaching_hours": "80",
            }
            report["checks"]["unknown_role_stays_blank"] = True
            report["checks"]["dedup_and_subject_filtered_sum_80"] = True
            upload(OUT / "materials/05_增量聘任.txt")
            await wait_tasks(1)
            incremental = await invoke("list_knowledge_tasks")
            assert len(incremental["items"]) == 1
            save("incremental-tasks.json", incremental)
            stale = await invoke("read_form_draft", {"draft_id": before["id"]})
            assert stale["stale"] and stale["answers"] == []
            added = await extract_pending()
            for fid in added:
                browser.post(
                    f"/v1/knowledge/facts/{fid}/decisions", json={"action": "accept", "expected_revision": 1}
                ).raise_for_status()
            final = await invoke("prepare_form", {"title": "教师资料核对", "fields": fields})
            assert {a["key"]: a["value"] for a in final["answers"]} == {**values, "job_title": "教授"}
            save("draft-final.json", final)
            catalog = await invoke("read_catalog", {"limit": 100})
            assert catalog == browser.get("/v1/catalog?limit=100").json()
            original = next(e for e in catalog["items"] if e["id"] == entry["id"])
            assert original["title"] == "聘任材料 · 人工命名" and original["note"] == "保留这条人工备注"
            (OUT / "results/共用知识目录.md").write_text(catalog["markdown"])
            save("catalog-final.json", catalog)
            report["checks"].update(
                {
                    "one_incremental_task": True,
                    "manual_facts_and_catalog_preserved": True,
                    "effective_date_updates_title": True,
                    "stale_draft_rejected": True,
                    "browser_api_and_mcp_same_catalog": True,
                }
            )
            template = OUT / "教师资料核对表_模板.docx"
            word = browser.post(
                f"/v1/forms/drafts/{final['id']}/document",
                files={"file": (template.name, template.read_bytes())},
            )
            word.raise_for_status()
            (OUT / "results/教师资料核对表_已填写.docx").write_bytes(word.content)
            report["checks"]["word_downloaded_from_same_draft"] = True
            save("mcp-receipts.json", receipts)
            save("verification.json", report)
            print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
