"""Two independent real MCP processes over the live Tencent-backed API (synthetic only)."""
import asyncio
import json
import os
import secrets
import sys
from uuid import uuid4

import httpx
from akb.config import ROOT
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def payload(result):
    assert not result.isError, result
    return result.structuredContent or json.loads(next(c.text for c in result.content if c.type == "text"))


async def main():
    folder = ROOT / "work/tencent-memory/smoke"
    folder.mkdir(exist_ok=True, mode=0o700)
    path = folder / "session.json"
    if not path.exists():
        with httpx.Client(base_url="http://127.0.0.1:5876", trust_env=False) as api:
            r = api.post("/v1/auth/register", json={"email": f"memory-smoke-{uuid4().hex}@example.test",
                "password": secrets.token_urlsafe(32), "display_name": "腾讯 MCP 合成探针"})
            r.raise_for_status()
            api.headers["X-CSRF-Token"] = r.json()["csrf"]
            context = api.get("/v1/context").json()
            r = api.post("/v1/tokens", json={"label": "Synthetic memory smoke", "scopes": ["knowledge:read", "knowledge:write", "profile:read"]})
            r.raise_for_status()
            state = {"token": r.json()["token"], "tenant": context["knowledge_base_id"]}
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
                json.dump(state, out)
    state = json.loads(path.read_text())
    params = StdioServerParameters(command=sys.executable, args=["-m", "akb.mcp_server"], cwd=str(ROOT),
        env={**os.environ, "AKB_API_URL": "http://127.0.0.1:5876", "AKB_TOKEN": state["token"], "AKB_EXPECTED_KB_ID": state["tenant"]})

    async def run(action):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await action(session)

    async def create(session):
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        assert "save_library_item" in names and "accept_knowledge_facts" not in names
        result = await session.call_tool("get_knowledge_context", {"expected_knowledge_base_id": state["tenant"]})
        assert payload(result)["knowledge_engine"] == "tencent"
        result = await session.call_tool("save_library_item", {"item": {"request_id": str(uuid4()),
            "kind": "preference", "key": "smoke-" + uuid4().hex[:8], "title": "合成偏好",
            "content": "探针要求按青禾主题归档，并在结论之后列明出处。", "expected_revision": 0}})
        assert not result.isError, result
        return {"id": payload(result)["id"], "tools": names}

    created = await run(create)

    async def recall(session):
        result = await session.call_tool("read_library_item", {"item_id": created["id"]})
        assert "青禾" in payload(result)["content"]
        result = await session.call_tool("search_library", {"query": "结论"})
        assert created["id"] in [r["id"] for r in payload(result)["items"]]
        # Record the separate proper-name recall probe, including an upstream miss.
        result = await session.call_tool("search_library", {"query": "青禾"})
        return created["id"] in [r["id"] for r in payload(result)["items"]]

    proper_name_hit = await run(recall)
    report = {"result": "PASS", "mcp_processes": 2, "tool_count": len(created["tools"]),
              "tools": created["tools"], "item_id": created["id"], "model_test": False,
              "checks": ["live identity binding", "retired tools absent", "real Tencent write", "cross-process read/search"],
              "proper_name_recall": {"query": "青禾", "hit": proper_name_hit,
                  "limitation": "Native Chinese tokenization can miss short proper names; direct ID reads remain available."}}
    (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
