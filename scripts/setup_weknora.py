"""Fetch a checksum-pinned WeKnora tree, apply reviewed patches and build privately."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from akb.config import ROOT


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "work/weknora-runtime")
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    spec = json.loads((ROOT / "integrations/weknora/upstream.json").read_text())
    patch = ROOT / "integrations/weknora/external-executor.patch"
    target = args.directory.resolve()
    target.mkdir(parents=True, exist_ok=True)
    archive = args.archive or target / "upstream.tar.gz"
    if not archive.exists():
        pending = archive.with_suffix(".download")
        url = f"https://codeload.github.com/Tencent/WeKnora/tar.gz/{spec['commit']}"
        with urllib.request.urlopen(url, timeout=60) as response, pending.open("wb") as output:
            shutil.copyfileobj(response, output)
        if sha(pending) != spec["archive_sha256"]:
            raise SystemExit("下载摘要不匹配；保留文件诊断，不继续构建")
        pending.rename(archive)
    if sha(archive) != spec["archive_sha256"]:
        raise SystemExit("源码压缩包摘要不匹配")
    source = target / ("WeKnora-" + spec["commit"])
    if source.exists():
        raise SystemExit("源码目录已存在，使用新的 --directory；不覆盖已有安装或本地修改")
    with tarfile.open(archive) as bundle:
        bundle.extractall(target, filter="data")
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=source, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=source, check=True)
    binary = target / "weknora"
    env = {**os.environ, "CGO_ENABLED": "1", "GOMAXPROCS": "3"}
    subprocess.run(["go", "build", "-p", "3", "-tags", "sqlite_fts5", "-ldflags",
        "-X github.com/Tencent/WeKnora/internal/handler.Edition=lite -X google.golang.org/protobuf/reflect/protoregistry.conflictPolicy=warn",
        "-o", str(binary), "./cmd/server"], cwd=source, env=env, check=True)
    manifest = {**spec, "patch_sha256": sha(patch), "binary_sha256": sha(binary), "source": str(source),
                "model_acceptance": "NOT_RUN", "campus_deployment": "NOT_RUN"}
    (target / "build.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("WeKnora external-executor binary built; no user data migrated")


if __name__ == "__main__":
    main()
