"""Validate local packaging and real MCP transport with a disposable synthetic identity, not an LLM."""

import asyncio
import importlib.util
import json
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from zipfile import ZipFile

import httpx
from akb.config import ROOT, Settings
from akb.onboarding import SKILL_VERSION
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    settings = Settings()
    base = f"http://127.0.0.1:{settings.api_port}"
    evidence = ROOT / "work/workbuddy-one-click-acceptance" / uuid4().hex
    evidence.mkdir(parents=True, mode=0o700)
    report = {"test_kind": "synthetic package and MCP protocol; not WorkBuddy model acceptance"}
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as browser:
        response = browser.post("/v1/auth/register", json={
            "email": f"package-{uuid4().hex}@example.test", "password": secrets.token_urlsafe(32),
            "display_name": "接入包合成验证空间"})
        response.raise_for_status()
        browser.headers["X-CSRF-Token"] = response.json()["csrf"]
        try:
            launch = browser.post("/v1/onboarding/package/launch")
            launch.raise_for_status()
            value = launch.json()
            kb = value["knowledge_base_id"]
            report["knowledge_base_id"] = kb
            assert urlsplit(value["launch_url"]).netloc == "task"
            assert "join_" not in value["launch_url"]
            original = next((settings.workbuddy_package_dir / kb).glob("*.zip"))
            spec = importlib.util.spec_from_file_location("local_expert_installer", ROOT / "scripts/install_workbuddy_package.py")
            installer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(installer)
            host = evidence / "isolated-workbuddy"
            receipt = installer.install(original, host)
            name = receipt["expert_name"]
            expert = host / "plugins/marketplaces/my-experts/plugins" / name
            validators = sorted((Path.home() / ".workbuddy/plugins/cache/workbuddy-builtin/skill-expert-manager")
                                .glob("*/scripts/validate_expert.py"))
            if validators:
                checked = subprocess.run([str(ROOT / ".venv/bin/python"), str(validators[-1]), str(expert)],
                                         env={**os.environ, "WORKBUDDY_CONFIG_DIR": str(host)},
                                         capture_output=True, text=True, timeout=30)
                assert checked.returncode == 0, checked.stdout
                report["workbuddy_bundled_expert_validator"] = "PASS"
            else:
                report["workbuddy_bundled_expert_validator"] = "NOT_RUN: WorkBuddy validator not installed"
            with ZipFile(original) as archive:
                config = json.loads(archive.read(name + "/.mcp.json"))["mcpServers"][name]
                report["archive_files"] = len(archive.namelist())
            store = evidence / "private-credentials"

            async def run(resume=False):
                env = {**config["env"], "AKB_CONNECTOR_STORE": str(store)}
                if resume:
                    env["AKB_JOIN_CODE"] = ""
                server = StdioServerParameters(command=config["command"], args=config["args"],
                                               cwd=config["cwd"], env=env)
                with (evidence / "mcp-stderr.log").open("a") as errors:
                    async with stdio_client(server, errlog=errors) as (read, write), ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        names = {tool.name for tool in tools.tools}
                        assert len(tools.tools) == (35 if settings.knowledge_engine == "tencent" else 38)
                        if settings.knowledge_engine == "tencent":
                            assert {"read_library", "save_library_item", "prepare_filling", "reparse_source"} <= names
                            assert not {"accept_knowledge_facts", "get_profile"} & names
                        report["mcp_tools"] = len(tools.tools)

                        async def invoke(tool, arguments=None):
                            result = await session.call_tool(tool, arguments or {})
                            assert not result.isError, f"{tool} returned an error"
                            return json.loads(result.content[0].text)

                        context = await invoke("get_knowledge_context", {"expected_knowledge_base_id": kb})
                        assert context["knowledge_base_id"] == kb and context["binding_configured"]
                        await invoke("start_onboarding", {"skill_version": SKILL_VERSION})
                        state = await invoke("read_onboarding")
                        assert state["connected"]
                        if resume:
                            assert state["answers"]["preferred_name"] == "合成验证员"
                            assert state["completed_at"]
                            return
                        assert state["next_question"]["key"] == "preferred_name"
                        state = await invoke("update_onboarding", {"progress": {
                            "expected_revision": state["revision"], "answers": {
                                "preferred_name": "合成验证员", "identity": "合成测试身份", "goal": "整理资料"},
                            "complete": True}})
                        assert state["completed_at"] and not state["pending_questions"]
                        if settings.knowledge_engine == "tencent":
                            library = await invoke("read_library")
                            assert not library["items"]
                        else:
                            profile = await invoke("get_profile")
                            assert not profile["candidates"] and all(v["value"] is None for v in profile["fields"].values())
            await run()
            await run(resume=True)
            report.update({"first_boot_and_binding": "PASS", "restart_without_join_code": "PASS",
                           "interview_resume": "PASS", "automatic_profile_facts": False,
                           "actual_workbuddy_model_interview": "NOT_RUN"})
        finally:
            for credential in browser.get("/v1/tokens").json():
                if not credential["revoked"]:
                    browser.delete("/v1/tokens/" + credential["id"]).raise_for_status()
            assert not browser.get("/v1/onboarding").json()["connected"]
            browser.post("/v1/auth/logout").raise_for_status()
            report["synthetic_credentials_revoked"] = True
            (evidence / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"report": str(evidence / "verification.json"), **report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
