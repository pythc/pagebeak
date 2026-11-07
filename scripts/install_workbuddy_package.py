"""Register only this project's local expert package. WorkBuddy owns MCP import and trust."""

import argparse
import fcntl
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zipfile import BadZipFile, ZipFile

from akb.config import ROOT
from akb.workbuddy_package import package_identity

MARKER = "zhixu-managed-local-v1\n"


def without_expiry(content):
    return [line for line in content.decode().splitlines() if not line.startswith("首次连接码有效至：")]


def code_free_config(content):
    config = json.loads(content)
    for server in config["mcpServers"].values():
        server["env"].pop("AKB_JOIN_CODE", None)
    return config


def ensure_directory(path):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError("安装目录不是普通目录，已保留原状")
    if path.is_dir():
        return
    ensure_directory(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if path.is_symlink() or not path.is_dir():
            raise


def inspect_package(archive_path):
    with ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > 100 or sum(e.file_size for e in entries) > 2 * 1024**2:
            raise ValueError("接入包超出本机验证包的大小限制")
        files = {}
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("接入包包含不允许的路径，已停止")
            if entry.is_dir():
                continue
            if len(path.parts) < 2 or entry.filename in files:
                raise ValueError("接入包结构不正确或包含重复文件")
            files[entry.filename] = archive.read(entry)
    roots = {PurePosixPath(p).parts[0] for p in files}
    if len(roots) != 1:
        raise ValueError("接入包应只有一个根目录")
    name = roots.pop()
    contents = {p[len(name) + 1:]: b for p, b in files.items()}
    try:
        manifest = json.loads(contents[".codebuddy-plugin/plugin.json"])
        config = json.loads(contents[".mcp.json"])["mcpServers"]
        server = config[name]
        kb = str(UUID(server["env"]["AKB_EXPECTED_KB_ID"]))
        if (name != package_identity(kb) or manifest["name"] != name or set(config) != {name}
                or server["command"] != sys.executable
                or server["cwd"] != str(ROOT) or server["args"] != ["-m", "akb.workbuddy_entry"]
                or set(server["env"]) != {"AKB_API_URL", "AKB_EXPECTED_KB_ID", "AKB_JOIN_CODE"}
                or not server["env"]["AKB_JOIN_CODE"].startswith("join_")):
            raise ValueError("接入包不属于当前本机项目，已停止")
        url = urlsplit(server["env"]["AKB_API_URL"])
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or url.username or url.password or url.path or url.query or url.fragment):
            raise ValueError("本机接入包只能连接回环服务")
        for required in ["agents/knowledge-steward.md", "skills/personal-knowledge/SKILL.md",
                         "skills/personal-knowledge/references/onboarding.md", "avatars/zhixu.svg"]:
            if required not in contents:
                raise ValueError("接入包缺少技能、管家或图标文件")
        # Only maintained files from this project's generator are accepted; no arbitrary hooks or code.
        from akb.config import Settings
        from akb.onboarding import SKILL_VERSION
        from akb.workbuddy_package import build_package
        expected_zip, _ = build_package(Settings(api_port=url.port or 80), kb, server["env"]["AKB_JOIN_CODE"],
                                        datetime.now(UTC), SKILL_VERSION)
        with ZipFile(BytesIO(expected_zip)) as expected_archive:
            expected = {p[len(name) + 1:]: expected_archive.read(p) for p in expected_archive.namelist()}
        # The generated expiry note is descriptive; it is the only timestamp-dependent file.
        for key in contents:
            if key != "README.md" and contents[key] != expected.get(key):
                raise ValueError("接入包与当前可信项目文件不一致，请重新生成")
        if without_expiry(contents["README.md"]) != without_expiry(expected["README.md"]):
            raise ValueError("接入包说明与可信项目文件不一致")
        if set(contents) != set(expected):
            raise ValueError("接入包包含不受支持的额外文件")
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError("接入包内容不完整或格式不正确") from None
    return name, manifest, contents


def install(archive_path, workbuddy_dir):
    name, manifest, contents = inspect_package(archive_path)
    market = workbuddy_dir / "plugins/marketplaces/my-experts"
    plugins = market / "plugins"
    ensure_directory(plugins)
    ensure_directory(market / ".codebuddy-plugin")
    registry = market / ".codebuddy-plugin/marketplace.json"
    destination = plugins / name
    lock = market / ".zhixu-package.lock"
    if registry.is_symlink() or lock.is_symlink():
        raise ValueError("注册文件是符号链接，已保留原状")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        original = registry.read_bytes() if registry.exists() else None
        value = json.loads(original) if original is not None else {"name": "my-experts", "plugins": []}
        if not isinstance(value, dict) or not isinstance(value.get("plugins"), list):
            raise ValueError("现有专家注册格式异常，已保留原状")
        existing = next((p for p in value["plugins"] if isinstance(p, dict) and p.get("name") == name), None)
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise ValueError("同名专家路径异常，未覆盖")
        if destination.exists():
            marker = destination / ".zhixu-managed-package"
            if not marker.is_file() or marker.is_symlink() or marker.read_text() != MARKER:
                raise ValueError("同名专家已经存在且不属于本安装器，未覆盖")
            allowed = set(contents) | {".zhixu-managed-package", ".created-by-session", ".created-by-session.archived"}
            for path in destination.rglob("*"):
                if path.is_symlink() or (path.is_file() and path.relative_to(destination).as_posix() not in allowed):
                    raise ValueError("已有专家包含人工文件，已保留原样，请先检查")
            for relative, data in contents.items():
                previous = (destination / relative).read_bytes()
                same = (code_free_config(previous) == code_free_config(data) if relative == ".mcp.json"
                        else without_expiry(previous) == without_expiry(data) if relative == "README.md"
                        else previous == data)
                if not same:
                    raise ValueError("已有专家内容已修改或版本不同，未覆盖")
            if not existing or existing.get("source") != "./plugins/" + name:
                raise ValueError("已有专家注册不匹配，未覆盖")
        elif existing:
            raise ValueError("同名专家注册已经存在但目录缺失，请在宿主检查")
        staging = Path(tempfile.mkdtemp(prefix=".zhixu-stage-", dir=plugins))
        committed = False
        written_registry = None
        backup_directory = None
        try:
            for relative, data in contents.items():
                path = staging / relative
                ensure_directory(path.parent)
                path.write_bytes(data)
                path.chmod(0o600)
            (staging / ".zhixu-managed-package").write_text(MARKER)
            # Only use a genuine host-injected creating session; never invent one to bypass adoption.
            session_id = os.environ.get("CODEBUDDY_SESSION_ID", "").strip()
            if session_id:
                UUID(session_id)
                (staging / ".created-by-session").write_text(session_id)
            if (registry.read_bytes() if registry.exists() else None) != original:
                raise ValueError("安装期间专家注册已变化，已停止")
            if destination.exists():
                backup_directory = plugins / (".zhixu-backup-" + uuid4().hex)
                destination.rename(backup_directory)
            staging.rename(destination)
            committed = True
            if existing is None:
                value["plugins"].append({"name": name, "source": "./plugins/" + name,
                                         "description": manifest["description"]})
            fd, temp_registry = tempfile.mkstemp(prefix=".zhixu-registry-", dir=registry.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(value, stream, ensure_ascii=False, indent=2)
                written_registry = Path(temp_registry).read_bytes()
                if (registry.read_bytes() if registry.exists() else None) != original:
                    raise ValueError("安装期间专家注册已变化，已停止")
                if original is not None:
                    # Backup contains only the pre-existing registry, never a newly-issued credential.
                    backup = registry.with_name("marketplace.json.zhixu-backup-" + uuid4().hex)
                    backup.write_bytes(original)
                    backup.chmod(0o600)
                os.replace(temp_registry, registry)
            finally:
                Path(temp_registry).unlink(missing_ok=True)
            return {"status": "registered_waiting_for_host", "expert_name": name,
                    "knowledge_base_id": json.loads(contents[".mcp.json"])["mcpServers"][name]["env"]["AKB_EXPECTED_KB_ID"],
                    "session_marker_present": bool(session_id),
                    "skill_path": str(destination / "skills/personal-knowledge/SKILL.md"),
                    "next": "在当前对话读取知识库管家 Skill，按需连接本库 MCP，实际核验后直接完成初始化并继续整理资料。相关信息可合并询问；无需回网页点击初始化、再次召唤专家或另开对话"}
        except BaseException:
            if written_registry and registry.exists() and registry.read_bytes() == written_registry:
                if original is None:
                    registry.unlink()
                else:
                    fd, recovery = tempfile.mkstemp(prefix=".zhixu-recovery-", dir=registry.parent)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(original)
                    os.replace(recovery, registry)
            if committed:
                shutil.rmtree(destination)
            if backup_directory:
                backup_directory.rename(destination)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--workbuddy-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = install(args.archive, args.workbuddy_dir.expanduser())
    except (ValueError, OSError, BadZipFile):
        raise SystemExit("接入包注册未完成。请检查本机服务版本、目录权限及已有同名专家；原有连接和信任设置保持原状。") from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
