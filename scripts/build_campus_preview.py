"""Bundle reviewed source and native frontend assets, excluding all user/runtime data."""
import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path

from akb.config import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assets = args.frontend.resolve()
    version = json.loads((assets / "version.json").read_text())
    if version.get("version") != "4.19.1" or not version.get("revision", "").startswith("zhixu-"):
        raise SystemExit("需要已校验的定制前端产物")
    if args.output.exists():
        raise SystemExit("输出已存在，保留原包；请指定新路径")
    files = {}
    for folder in ["backend/akb", "migrations", "deploy/campus", "integrations"]:
        for item in (ROOT / folder).rglob("*"):
            relative = item.relative_to(ROOT)
            if item.is_symlink() or any(p.startswith(".") or p in {"__pycache__", "node_modules", "demo"}
                                        for p in relative.parts):
                continue
            if item.is_file():
                files["source/" + relative.as_posix()] = item
    names = ["pyproject.toml", "uv.lock", "alembic.ini", "compose.yaml", ".env.example", ".dockerignore",
             "README.md", "AGENTS.md", "docs/teacher-assistant-20261026.md", "docs/教师助理试点指南.md",
             "docs/新手使用教程.md"]
    scripts = ["bootstrap", "build_beginner_guide", "build_cloudreve_frontend", "build_teacher_assistant",
               "build_campus_preview", "dev", "install_workbuddy", "install_workbuddy_package", "manage_admin",
               "run_tencent_memory", "setup_cloudreve", "setup_local", "setup_tencent_memory", "workbuddy_config", "zhixu"]
    names += ["scripts/" + name + ".py" for name in scripts]
    files.update({"source/" + name: ROOT / name for name in names})
    for item in assets.rglob("*"):
        if item.is_symlink():
            raise SystemExit("前端产物不能含链接")
        if item.is_file():
            files["frontend-statics/" + item.relative_to(assets).as_posix()] = item
    archive = ROOT / "work/cloudreve/frontend-4.19.1.tar.gz"
    upstream = json.loads((ROOT / "integrations/cloudreve/upstream.json").read_text())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != upstream["archive_sha256"]:
        raise SystemExit("前端上游源码校验失败")
    files["upstream/cloudreve-frontend-4.19.1.tar.gz"] = archive
    manifest = {"release": "0.9.0-preview", "published": False, "campus_deployment": "NOT_RUN",
                "workbuddy_native_model": "NOT_RUN", "frontend": version,
                "files": {name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for name, path in sorted(files.items())}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # No live .env, secrets, user files, runtime data, tests, or private WorkBuddy config.
    with args.output.open("xb") as output, tarfile.open(fileobj=output, mode="w:gz") as bundle:
        for name, path in sorted(files.items()):
            info = tarfile.TarInfo(name)
            payload = path.read_bytes()
            info.size, info.mode = len(payload), 0o644
            bundle.addfile(info, io.BytesIO(payload))
        payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mode = len(payload), 0o644
        bundle.addfile(info, io.BytesIO(payload))
    print(json.dumps({"path": str(args.output), "files": len(files),
                      "bytes": args.output.stat().st_size,
                      "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
