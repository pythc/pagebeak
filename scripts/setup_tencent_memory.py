"""Prepare checksum-pinned Tencent sources and Node 22 on approved platforms.

No global runtime, OpenClaw installation, host trust or network settings are changed.
Use --proxy only for this download/install process if required by your network.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from akb.config import ROOT
from akb.tencent_runtime import runtime_target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy")
    parser.add_argument("--cache-dir", type=Path, help="Archive cache; defaults to the existing local runtime directory")
    parser.add_argument("--check", action="store_true", help="Verify cached archives, installed lockfiles and SQLite without modifying the live runtime")
    args = parser.parse_args()
    target = runtime_target()
    integration = ROOT / "integrations/tencent-memory"
    manifest = json.loads((integration / "upstream.json").read_text())
    work = ROOT / "work/tencent-memory"
    work.mkdir(parents=True, exist_ok=True)
    cache = args.cache_dir or work
    cache.mkdir(parents=True, exist_ok=True)

    def download(url, name, checksum):
        target = cache / name
        if not target.exists():
            if args.check:
                raise SystemExit(f"Missing cached archive: {name}")
            temp = target.with_suffix(".download")
            command = ["curl", "-fL", "--retry", "2", "--retry-all-errors", "--retry-delay", "1",
                       "--connect-timeout", "15", "--max-time", "180"]
            command += ["--proxy", args.proxy] if args.proxy else ["--noproxy", "*"]
            subprocess.run([*command, url, "-o", str(temp)], check=True)
            if hashlib.sha256(temp.read_bytes()).hexdigest() != checksum:
                raise SystemExit("Download checksum mismatch; preserving existing installation")
            temp.replace(target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != checksum:
            raise SystemExit("Cached archive checksum mismatch")
        return target

    commit, version = manifest["upstream_commit"], manifest["node_version"]
    archive = download(f"https://codeload.github.com/TencentCloud/TencentDB-Agent-Memory/tar.gz/{commit}", "upstream.tar.gz", manifest["archive_sha256"])
    suffix = "" if target == "darwin-arm64" else "-" + target
    node_archive = download(f"https://nodejs.org/dist/v{version}/node-v{version}-{target}.tar.gz", "node22" + suffix + ".tar.gz", manifest["node_" + target.replace("-", "_") + "_sha256"])
    native_checksum = manifest["sqlite_prebuild_sha256" if target == "darwin-arm64" else "sqlite_linux_x64_prebuild_sha256"]
    native = download(f"https://github.com/WiseLibs/better-sqlite3/releases/download/v11.10.0/better-sqlite3-v11.10.0-node-v127-{target}.tar.gz", "better-sqlite3-node22" + suffix + ".tar.gz", native_checksum)
    upstream = work / ("TencentDB-Agent-Memory-" + commit)
    node_root = work / f"node-v{version}-{target}"
    for path, target in [(archive, upstream), (node_archive, node_root)]:
        if not target.exists():
            if args.check:
                raise SystemExit(f"Missing extracted runtime: {target.name}")
            with tarfile.open(path) as bundle:
                bundle.extractall(work, filter="data")
    env = {**os.environ, "PATH": str(node_root / "bin") + os.pathsep + os.environ.get("PATH", "")}
    for part in ("MemoryCore", "MemoryKnowledge"):
        directory = upstream / part
        lock = integration / (part + "-package-lock.json")
        expected = hashlib.sha256(lock.read_bytes()).hexdigest()
        marker = directory / ".zhixu-installed-lock"
        if args.check:
            if hashlib.sha256((directory / "package-lock.json").read_bytes()).hexdigest() != expected:
                raise SystemExit(f"Installed lockfile differs: {part}")
            continue
        if not marker.exists() or marker.read_text() != expected:
            shutil.copy2(lock, directory / "package-lock.json")
            command = [str(node_root / "bin/node"), str(node_root / "lib/node_modules/npm/bin/npm-cli.js"),
                       "ci", "--ignore-scripts", "--legacy-peer-deps", "--no-audit", "--no-fund"]
            if args.proxy:
                command += ["--proxy=" + args.proxy, "--https-proxy=" + args.proxy]
            subprocess.run(command, cwd=directory, env=env, check=True)
            marker.write_text(expected)
    if not args.check:
        with tarfile.open(native) as bundle:
            bundle.extractall(upstream / "MemoryKnowledge/node_modules/better-sqlite3", filter="data")
    subprocess.run([str(node_root / "bin/node"), "-e", "const d=require('better-sqlite3')(':memory:');d.exec('CREATE VIRTUAL TABLE t USING fts5(body)');d.close();console.log('SQLite FTS5 verified')"], cwd=upstream / "MemoryKnowledge", check=True)
    print("Ready. Start with .venv/bin/python scripts/dev.py and AKB_KNOWLEDGE_ENGINE=tencent.")


if __name__ == "__main__":
    main()
