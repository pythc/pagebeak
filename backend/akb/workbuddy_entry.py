"""First-launch binding for a local expert package. Stdout belongs exclusively to MCP."""

import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx


class ConnectionError(ValueError):
    pass


class AuthorizationExpired(ConnectionError):
    pass


def request(base, path, body=None, token=None, kb=None):
    headers = {"Authorization": "Bearer " + token, "X-AKB-Knowledge-Base": kb} if token else {}
    try:
        with httpx.Client(base_url=base, timeout=25, follow_redirects=False, trust_env=False) as client:
            response = client.request("POST" if body is not None else "GET", path, json=body, headers=headers)
    except httpx.HTTPError:
        raise ConnectionError("本机知识库暂不可达，请先启动服务后重试") from None
    if response.status_code >= 300:
        if response.status_code in {401, 403}:
            raise AuthorizationExpired("连接授权已失效，请返回开始页生成新的本机接入包")
        raise ConnectionError(f"知识库请求未完成（HTTP {response.status_code}），请返回开始页检查接入状态")
    try:
        return response.json() if response.status_code != 204 else None
    except ValueError:
        raise ConnectionError("知识库返回了无法读取的响应") from None


def private_directory(path):
    if path.is_symlink():
        raise ConnectionError("凭据目录是符号链接，已停止接入")
    if path.exists():
        if not path.is_dir():
            raise ConnectionError("凭据目录路径被其他文件占用")
        if path.stat().st_mode & 0o077:
            raise ConnectionError("凭据目录权限不是仅本人可访问，请检查后重试")
        return
    # Only the leaf is private; its existing ancestors need not be private.
    parent = path.parent
    missing = []
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for ancestor in reversed(missing):
        ancestor.mkdir(mode=0o700)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        private_directory(path)


def write_private(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".zhixu-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def bind(base, kb, code, directory):
    """Do not edit WorkBuddy files; the host owns dependency installation and trust."""
    from akb.onboarding import SKILL_VERSION

    kb = str(UUID(kb))
    base = base.rstrip("/")
    url = urlsplit(base)
    if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.username or url.password or url.query or url.fragment or url.path):
        raise ConnectionError("当前接入包仅供同一台电脑的本地验收使用")
    private_directory(directory)
    cache = directory / (kb + ".json")
    lock = directory / (kb + ".lock")
    if cache.is_symlink() or lock.is_symlink():
        raise ConnectionError("凭据文件是符号链接，已停止接入")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        token = None
        if cache.exists():
            if cache.stat().st_mode & 0o077:
                raise ConnectionError("凭据文件权限不是仅本人可访问，已停止接入")
            try:
                saved = json.loads(cache.read_text())
            except (ValueError, OSError):
                raise ConnectionError("现有连接凭据无法读取，已保留原文件") from None
            if saved.get("knowledge_base_id") != kb or saved.get("base_url") != base:
                raise ConnectionError("现有凭据与目标知识库不符，已停止接入")
            candidate = saved.get("token", "")
            if not isinstance(candidate, str) or not candidate.startswith("akb_"):
                raise ConnectionError("现有连接凭据格式不正确，已保留原文件")
            try:
                context = request(base, "/v1/context", token=candidate, kb=kb)
                if context["knowledge_base_id"] != kb:
                    raise ConnectionError("连接返回了其他知识库，已停止接入")
                token = candidate
            except AuthorizationExpired:
                if not code.startswith("join_"):
                    raise
            except ConnectionError:
                # A network failure must not silently rotate credentials or consume a ticket.
                raise
        if token is None:
            if not code.startswith("join_"):
                raise ConnectionError("接入包缺少有效的一次性连接码，请回开始页重新下载")
            result = request(base, "/v1/onboarding/redeem", {"code": code, "knowledge_base_id": kb})
            token = result["config"]["env"]["AKB_TOKEN"]
            try:
                if result["config"]["env"]["AKB_EXPECTED_KB_ID"] != kb:
                    raise ConnectionError("兑换返回了其他知识库，已停止接入")
                context = request(base, "/v1/context", token=token, kb=kb)
                if context["knowledge_base_id"] != kb:
                    raise ConnectionError("连接身份不匹配，已停止接入")
                write_private(cache, {"knowledge_base_id": kb, "base_url": base, "token": token})
            except Exception:
                try:
                    request(base, "/v1/auth/logout", {}, token, kb)
                except ConnectionError:
                    pass
                raise
        request(base, "/v1/onboarding/installation", {"skill_version": SKILL_VERSION, "channel": "expert"}, token, kb)
        return token


def main():
    try:
        base = os.environ.get("AKB_API_URL", "http://127.0.0.1:5876")
        kb = os.environ.get("AKB_EXPECTED_KB_ID", "")
        directory = Path(os.environ.get("AKB_CONNECTOR_STORE", str(Path.home() / ".zhixu-connectors")))
        token = bind(base, kb, os.environ.get("AKB_JOIN_CODE", ""), directory)
        os.environ["AKB_TOKEN"] = token
        os.environ.pop("AKB_JOIN_CODE", None)
        from akb.mcp_server import main as serve
        serve()
    except (ConnectionError, ValueError, OSError, KeyError, TypeError):
        # No traceback, response, token, code or complete environment in host logs.
        print("知序接入尚未完成。请保持本机服务运行，并返回开始页检查或重新生成接入包；已有配置不会被覆盖。", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
