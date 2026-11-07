"""Install this local workspace's connector without exposing its API credential."""
import argparse
import fcntl
import json
import os
import shutil
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "integrations/workbuddy/skills/personal-knowledge"
NEXT_STEP = ("直接在当前对话读取已安装的知识库管家 Skill，选用或按需连接本库 MCP，核验后继续初始化和整理资料。"
             "仅宿主要求授权或连接确实失败时处理必要操作；不需要回网页点击初始化或重复安装")


def ensure_directory(path):
    """Do not request mkdir on existing directories (some brokered hosts reject it)."""
    if path.is_symlink():
        raise ValueError("安装目录是符号链接，请保留原状并手动检查")
    if path.is_dir():
        return
    if path.exists():
        raise ValueError("安装路径已存在且不是目录，已保留原状")
    ensure_directory(path.parent)
    try:
        path.mkdir()
    except FileExistsError:
        if path.is_symlink() or not path.is_dir():
            raise


def report_installation(base, kb, server, version):
    return request(base, "/v1/onboarding/installation", {"skill_version": version},
                   server["env"]["AKB_TOKEN"], kb)


def inspect_installation(base, kb, target):
    """Inspect only this library, and report installed status without claiming MCP was selected."""
    validate_base(base)
    server_name = "zhixu-" + kb
    config = json.loads((target / "mcp.json").read_text())
    server = config.get("mcpServers", {}).get(server_name)
    if not isinstance(server, dict) or not isinstance(server.get("env"), dict):
        raise ValueError("尚未找到本知识库的安装配置，请回开始页面生成接入指令")
    env = server["env"]
    if env.get("AKB_EXPECTED_KB_ID") != kb or env.get("AKB_API_URL", "").rstrip("/") != base.rstrip("/"):
        raise ValueError("安装配置与指定知识库或服务地址不符，已停止核对")
    skill = target / "skills/personal-knowledge"
    if not all((skill / name).is_file() for name in [".zhixu-managed", "SKILL.md", "references/onboarding.md"]):
        raise ValueError("知识库管家技能未安装完整，请保留原文件并返回开始页面")
    receipt = json.loads((target / (server_name + ".installation.json")).read_text())
    context = request(base, "/v1/context", token=env["AKB_TOKEN"], kb=kb)
    if context["knowledge_base_id"] != kb:
        raise ValueError("当前连接指向其他知识库，已停止核对")
    state = report_installation(base, kb, server, receipt["skill_version"])
    return {"knowledge_base_id": kb, "server_name": server_name,
            "status": "connected" if state["connected"] else "installed_waiting_for_mcp_selection",
            "next": NEXT_STEP}


def atomic_write(path, data):
    descriptor, name = tempfile.mkstemp(prefix=".zhixu-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def request(base, path, body=None, token=None, kb=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers.update({"Authorization": "Bearer " + token, "X-AKB-Knowledge-Base": kb})
    req = Request(base + path, method="POST" if body is not None else "GET",
                  data=json.dumps(body).encode() if body is not None else None, headers=headers)
    # Do not follow redirects while carrying an access code or credential.
    from urllib.request import HTTPRedirectHandler, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    with build_opener(NoRedirect).open(req, timeout=25) as response:
        return json.loads(response.read()) if response.status != 204 else None


def validate_base(base):
    url = urlsplit(base)
    if url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
        raise ValueError("服务地址格式不正确")
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("仅本机允许 HTTP，其他地址需要 HTTPS")


def install(base, code, kb, target):
    validate_base(base)
    if target.is_symlink():
        raise ValueError("安装目录是符号链接，请由用户检查后手动安装")
    ensure_directory(target)
    config_path, skill_path = target / "mcp.json", target / "skills/personal-knowledge"
    receipt_path = target / ("zhixu-" + kb + ".installation.json")
    ensure_directory(skill_path.parent)
    if config_path.is_symlink() or skill_path.is_symlink() or skill_path.parent.is_symlink() or receipt_path.is_symlink():
        raise ValueError("配置或技能目录是符号链接，请保留原文件并手动安装")
    with (target / ".zhixu-install.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = config_path.read_bytes() if config_path.exists() else None
        original_receipt = receipt_path.read_bytes() if receipt_path.exists() else None
        config = json.loads(original) if original is not None else {"mcpServers": {}}
        if not isinstance(config, dict) or not isinstance(config.get("mcpServers", {}), dict):
            raise ValueError("现有连接配置格式不正确，已保留原样")
        marker = skill_path / ".zhixu-managed"
        if skill_path.exists() and not marker.is_file():
            raise ValueError("已存在同名技能，安装器不会覆盖；请先由用户检查或改名")
        # Prepare skill files before consuming the one-time ticket.
        staging_root = Path(tempfile.mkdtemp(prefix=".zhixu-skill-", dir=skill_path.parent))
        staged = staging_root / "personal-knowledge"
        backup_skill = None
        result = None
        written = None
        skill_installed = False
        written_receipt = None
        try:
            shutil.copytree(SKILL, staged)
            (staged / ".zhixu-managed").write_text("知序安装器管理的技能副本\n")
            result = request(base, "/v1/onboarding/redeem", {"code": code, "knowledge_base_id": kb})
            server = result["config"]
            token = server["env"]["AKB_TOKEN"]
            if server["env"]["AKB_EXPECTED_KB_ID"] != kb:
                raise ValueError("返回的知识库身份不匹配，安装已停止")
            context = request(base, "/v1/context", token=token, kb=kb)
            if context["knowledge_base_id"] != kb:
                raise ValueError("连接身份核验失败")
            if context.get("knowledge_engine") in {"tencent", "weknora"}:
                shutil.rmtree(staged)
                shutil.copytree(ROOT / "integrations/workbuddy" / (context["knowledge_engine"] + "-skills") / "personal-knowledge", staged)
                (staged / ".zhixu-managed").write_text("知序安装器管理的技能副本\n")
            # The installer is local-only; do not run a remote-provided command.
            if server["command"] != str(ROOT / ".venv/bin/python") or server["cwd"] != str(ROOT):
                raise ValueError("当前安装器仅支持与服务同一台电脑的本地开发版")
            previous = config.get("mcpServers", {}).get(result["server_name"])
            if previous is not None and (
                not isinstance(previous, dict)
                or not isinstance(previous.get("env"), dict)
                or previous["env"].get("AKB_EXPECTED_KB_ID") != kb
                or previous.get("cwd") != str(ROOT)
            ):
                raise ValueError("同名连接不属于本安装器，已保留原样")
            config.setdefault("mcpServers", {})[result["server_name"]] = server
            if (config_path.read_bytes() if config_path.exists() else None) != original:
                raise ValueError("安装期间连接配置已变化，请重新生成指令后重试")
            if original is not None:
                backup = target / ("mcp.json.zhixu-backup-" + uuid4().hex)
                atomic_write(backup, original)
            if skill_path.exists():
                backup_skill = skill_path.with_name(".personal-knowledge-backup-" + uuid4().hex)
                skill_path.rename(backup_skill)
            staged.rename(skill_path)
            skill_installed = True
            written = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode()
            atomic_write(config_path, written)
            receipt = {"knowledge_base_id": kb, "server_name": result["server_name"],
                       "credential_id": result["credential_id"], "skill_version": result["skill_version"],
                       "status": "installed_waiting_for_mcp_selection", "skill_path": str(skill_path),
                       "next": NEXT_STEP}
            written_receipt = json.dumps(receipt, ensure_ascii=False, indent=2).encode()
            atomic_write(receipt_path, written_receipt)
            report_installation(base, kb, server, result["skill_version"])
            return receipt
        except BaseException:
            if result:
                try:
                    request(base, "/v1/auth/logout", {}, result["config"]["env"]["AKB_TOKEN"], kb)
                except (HTTPError, URLError, TimeoutError):
                    pass
            if written and config_path.exists() and config_path.read_bytes() == written:
                if original is None:
                    config_path.unlink()
                else:
                    atomic_write(config_path, original)
            if written_receipt and receipt_path.exists() and receipt_path.read_bytes() == written_receipt:
                if original_receipt is None:
                    receipt_path.unlink()
                else:
                    atomic_write(receipt_path, original_receipt)
            if backup_skill:
                shutil.rmtree(skill_path, ignore_errors=True)
                backup_skill.rename(skill_path)
            elif skill_installed:
                shutil.rmtree(skill_path, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--code")
    parser.add_argument("--check", action="store_true", help="核对已有安装并回报页面，不重复兑换接入码")
    parser.add_argument("--workbuddy-dir", type=Path, default=Path.home() / ".workbuddy")
    args = parser.parse_args()
    if not args.check and not args.code:
        parser.error("安装需要 --code；核对已有安装请使用 --check")
    try:
        if args.check:
            receipt = inspect_installation(args.base_url.rstrip("/"), args.knowledge_base_id, args.workbuddy_dir)
        else:
            receipt = install(args.base_url.rstrip("/"), args.code, args.knowledge_base_id, args.workbuddy_dir)
    except HTTPError as exc:
        raise SystemExit(f"接入未完成（HTTP {exc.code}）。请回开始页重新生成指令；不要重复使用旧指令。") from None
    except json.JSONDecodeError:
        raise SystemExit("接入未完成：连接配置或服务响应不是有效 JSON，未覆盖现有配置。请保留原文件并检查手动连接设置。") from None
    except ValueError as exc:
        raise SystemExit(f"接入未完成：{exc}。请保留现有文件，按原因处理后重新生成指令。") from None
    except PermissionError:
        raise SystemExit("接入受阻：宿主未允许本次目录或文件操作。请通过 WorkBuddy 正常授权处理；不要关闭保护层、改变启动环境或跳过 Python 初始化来重试。") from None
    except (OSError, URLError, KeyError, TypeError) as exc:
        # Do not print response bodies, CLI args, credentials or tracebacks.
        raise SystemExit(f"接入未完成：{type(exc).__name__}。现有配置会保留，请检查本机服务、目录权限或已有同名技能。") from None
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
