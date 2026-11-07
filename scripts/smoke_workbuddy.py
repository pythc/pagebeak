"""Exercise the real stdio MCP protocol against the running local API with synthetic data."""

import asyncio
import json
import os
import secrets
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BASE = os.environ.get("AKB_API_URL", "http://127.0.0.1:5876")
if urlsplit(BASE).hostname not in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit("Synthetic preview setup only supports a loopback API")
EMAIL = f"preview-{uuid4().hex[:12]}@example.test"
PASSWORD = secrets.token_urlsafe(32)


async def main():
    with httpx.Client(base_url=BASE, timeout=30) as browser:
        result = browser.post(
            "/v1/auth/register",
            json={"email": EMAIL, "password": PASSWORD, "display_name": "陈舟 · 示例空间"},
        )
        if result.status_code == 409:
            result = browser.post("/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
        result.raise_for_status()
        browser.headers["X-CSRF-Token"] = result.json()["csrf"]
        sources = browser.get("/v1/sources").json()["items"]
        if not sources:
            documents = [
                (
                    "陈舟个人资料（合成示例）",
                    "姓名：陈舟\n工作单位：示例大学\n职称：讲师\n邮箱：chen@example.test\n研究方向：分布式系统与知识管理",
                    "self",
                ),
                (
                    "知识库项目说明（合成示例）",
                    "项目名称：个人资料知识库\n项目目标：将资料整理为可追溯的个人知识，支持表单准备。\n实施重点：多用户隔离、原文证据、人工更正、WorkBuddy 接入。",
                    "reference",
                ),
                (
                    "收藏论文笔记（合成示例）",
                    "本文仅为合成研究材料。\n论文标题：面向证据的知识检索方法\n作者：王示例\n研究主题：文档检索、证据定位和知识组织。\n这是一篇收藏资料，不是陈舟的发表成果。",
                    "reference",
                ),
            ]
            for title, content, scope in documents:
                browser.post(
                    "/v1/sources/text",
                    headers={"Idempotency-Key": uuid4().hex},
                    json={"title": title, "text": content, "subject_scope": scope},
                ).raise_for_status()
        for _ in range(100):
            sources = browser.get("/v1/sources").json()["items"]
            if all(s["status"] in {"ready", "partial"} for s in sources):
                break
            time.sleep(0.1)
        assert all(s["status"] in {"ready", "partial"} for s in sources), "Worker did not finish ingestion"
        token_response = browser.post(
            "/v1/tokens",
            json={
                "label": "MCP acceptance (synthetic)",
                "scopes": ["knowledge:read", "knowledge:write", "profile:read", "forms:write"],
            },
        )
        token_response.raise_for_status()
        token = token_response.json()
        try:
            server = StdioServerParameters(
                command=sys.executable,
                args=["-m", "akb.mcp_server"],
                env={"AKB_API_URL": BASE, "AKB_TOKEN": token["token"],
                     "AKB_EXPECTED_KB_ID": browser.get("/v1/context").json()["knowledge_base_id"]},
            )
            async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 38

                async def invoke(name, arguments=None):
                    result = await session.call_tool(name, arguments or {})
                    assert not result.isError, f"MCP tool {name} failed"
                    return json.loads(result.content[0].text)

                listing = await invoke("list_sources")
                assert len(listing["items"]) == 3
                job = await invoke("organize_catalog")
                for _ in range(100):
                    state = await invoke("get_job", {"job_id": job["job_id"]})
                    if state["status"] == "completed":
                        break
                    await asyncio.sleep(0.1)
                assert state["status"] == "completed"
                directory = await invoke("read_catalog")
                assert directory["total"] == 3 and directory["pending"] == 0
                assert directory["id"] == browser.get("/v1/catalog").json()["id"]
                found = await invoke("search_knowledge", {"query": "WorkBuddy"})
                assert found["items"]
                evidence = found["items"][0]
                exact = await invoke("read_evidence", {"evidence_id": evidence["id"]})
                assert exact["text"] == evidence["text"]
                profile = await invoke("get_profile")
                assert profile["fields"]["name"]["value"] == "陈舟"
                draft = await invoke(
                    "prepare_form",
                    {
                        "title": "WorkBuddy 验收表",
                        "fields": [
                            {"key": "name", "label": "姓名", "fact_key": "name"},
                            {"key": "count", "label": "论文数量", "fact_key": "publication_count"},
                        ],
                    },
                )
                assert [a["status"] for a in draft["answers"]] == ["ready", "missing"]
                if not browser.get("/v1/pages").json()["items"]:
                    await invoke(
                        "save_knowledge_page",
                        {
                            "title": "知识库项目专题（合成示例）",
                            "content": "项目实施重点：多用户隔离、原文证据、人工更正和 WorkBuddy 接入。",
                            "evidence_ids": [evidence["id"]],
                        },
                    )
                usage = await invoke("get_storage_usage")
                assert usage["used_bytes"] > 0 and usage["upload_limit_bytes"] == 2 * 1024**3
                candidates = await invoke("list_cleanup_candidates")
                target = next(s for s in candidates["items"] if s["subject_scope"] == "reference")
                cleanup_original = (await invoke("read_source", {"source_id": target["id"]}))["items"][0]
                plan = await invoke(
                    "propose_storage_cleanup",
                    {
                        "source_ids": [target["id"]],
                        "mode": "summarize",
                        "summaries": [
                            {
                                "source_id": target["id"],
                                "content": "验收归纳：" + cleanup_original["text"],
                                "citations": [
                                    {"evidence_id": cleanup_original["id"], "quote": cleanup_original["text"]}
                                ],
                            }
                        ],
                    },
                )
                assert "验收归纳" in plan["summary"]
                denied = await session.call_tool("execute_storage_cleanup", {"plan_id": plan["id"]})
                assert denied.isError
                assert (await invoke("read_storage_cleanup", {"plan_id": plan["id"]}))["status"] == "pending"
                browser.post(
                    f"/v1/storage/plans/{plan['id']}/approve", json={"acknowledge_loss": True}
                ).raise_for_status()
                executed = await invoke("execute_storage_cleanup", {"plan_id": plan["id"]})
                assert executed["status"] == "completed"
                for _ in range(50):
                    after = await invoke("get_storage_usage")
                    if after["used_bytes"] == usage["used_bytes"] - target["size"]:
                        break
                    await asyncio.sleep(0.1)
                assert after["used_bytes"] == usage["used_bytes"] - target["size"]
                archives = (await invoke("read_storage_archives"))["items"]
                assert any(
                    a["id"] == executed["result"]["archive_id"] and "验收归纳" in a["content"]
                    for a in archives
                )
                print("MCP protocol handshake: PASS")
                print(
                    "Tools exposed: 38; catalog/search/form/page/storage/authorized cleanup/archive calls: PASS"
                )
                print("Synthetic preview data: 3 sources; no real personal data used")
        finally:
            browser.delete(f"/v1/tokens/{token['id']}").raise_for_status()
            print("Acceptance token revoked after verification")


asyncio.run(main())
