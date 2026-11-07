"""Rebuild the pinned native frontend with our small patch; --deploy changes only local static assets."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from akb.config import ROOT


def activate_assets(source, revision, run):
    container = "agent-knowledge-base-cloudreve-1"
    stage = "/cloudreve/data/statics-stage-" + revision
    active = "/cloudreve/data/statics"
    backup = active + ".backup-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    run(["docker", "exec", container, "mkdir", "-p", stage])
    run(["docker", "cp", str(source / "build") + "/.", container + ":" + stage])
    previous = run(["docker", "exec", container, "test", "-d", active], check=False).returncode == 0
    moved, activated = False, False
    restart = ["docker", "compose", "--profile", "cloudreve", "restart", "cloudreve"]
    try:
        if previous:
            run(["docker", "exec", container, "mv", active, backup])
            moved = True
        run(["docker", "exec", container, "mv", stage, active])
        activated = True
        run(restart, cwd=ROOT)
    except Exception:
        if activated:
            run(["docker", "exec", container, "mv", active, stage + "-failed"])
        if moved:
            run(["docker", "exec", container, "mv", backup, active])
        if activated or moved:
            run(restart, cwd=ROOT)
        raise
    return {"revision": revision, "previous_statics": backup if previous else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy", action="store_true")
    args = parser.parse_args()
    subprocess.run([sys.executable, str(ROOT / "scripts/build_beginner_guide.py")], check=True)
    integration = ROOT / "integrations/cloudreve"
    metadata = json.loads((integration / "upstream.json").read_text())
    fingerprint = hashlib.sha256((integration / "frontend.patch").read_bytes())
    for item in sorted((integration / "overlay").rglob("*")):
        if item.is_file():
            fingerprint.update(str(item.relative_to(integration)).encode())
            fingerprint.update(item.read_bytes())
    revision = "zhixu-" + fingerprint.hexdigest()[:12]
    workspace = ROOT / "work/cloudreve-custom" / revision
    workspace.mkdir(parents=True, exist_ok=True)
    source, baseline = workspace / "source", workspace / "baseline"
    archive = ROOT / "work/cloudreve/frontend-4.19.1.tar.gz"

    def run(command, *, cwd=source, log="build.log", check=True, **kwargs):
        with (workspace / log).open("a") as output:
            return subprocess.run(
                command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, check=check, **kwargs
            )

    if not archive.exists():
        archive.parent.mkdir(parents=True, exist_ok=True)
        run(["curl", "-fsSL", "--max-time", "120", metadata["archive_url"], "-o", str(archive)], cwd=ROOT)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != metadata["archive_sha256"]:
        raise RuntimeError("Upstream archive checksum mismatch; refusing to build")

    def extract(destination):
        destination.mkdir()
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                relative = Path(*Path(member.name).parts[1:])
                if str(relative) == ".":
                    continue
                member.name = str(relative)
                bundle.extract(member, destination, filter="data")

    if not source.exists():
        extract(source)
        run(["patch", "--batch", "-p1", "-i", str(integration / "frontend.patch")])
        shutil.copytree(integration / "overlay", source, dirs_exist_ok=True)
        (source / ".zhixu-revision").write_text(revision)
    if (source / ".zhixu-revision").read_text() != revision:
        raise RuntimeError("Working source revision differs; preserving it")
    if not (source / "node_modules").exists():
        # Reuse this exact upstream lockfile's local installation when available.
        cache = ROOT / "work/cloudreve" / ("frontend-" + metadata["upstream_commit"])
        if (cache / "node_modules/.yarn-integrity").exists() and (cache / "yarn.lock").read_bytes() == (
            source / "yarn.lock"
        ).read_bytes():
            (source / "node_modules").symlink_to(cache / "node_modules", target_is_directory=True)
        else:
            print("Installing upstream dependencies from frozen yarn.lock", flush=True)
            run(
                [
                    "corepack",
                    "yarn",
                    "install",
                    "--frozen-lockfile",
                    "--ignore-scripts",
                    "--ignore-engines",
                    "--non-interactive",
                ]
            )

    print(f"Checking {revision} against the unchanged upstream TypeScript baseline", flush=True)
    if not baseline.exists():
        extract(baseline)
        (baseline / "node_modules").symlink_to(source / "node_modules", target_is_directory=True)
    diagnostic = re.compile(r"(src/[^\(\n]+)\(\d+,\d+\): error (TS\d+): (.*)")
    counts = []
    for folder, log in [(baseline, "types-upstream.log"), (source, "types-custom.log")]:
        (workspace / log).write_text("")
        result = run(
            [str(source / "node_modules/.bin/tsc"), "--noEmit", "-p", str(folder / "tsconfig.json")],
            log=log,
            check=False,
        )
        counter = Counter(diagnostic.findall((workspace / log).read_text()))
        if result.returncode and not counter:
            raise RuntimeError("TypeScript check failed without diagnostics; inspect local log")
        counts.append(counter)
    type_report = {
        "upstream_errors": sum(counts[0].values()),
        "custom_errors": sum(counts[1].values()),
        "new_errors": sum((counts[1] - counts[0]).values()),
    }
    (workspace / "typecheck.json").write_text(json.dumps(type_report, indent=2))
    if type_report["new_errors"]:
        raise RuntimeError("New TypeScript errors; refusing to deploy")
    print(f"TypeScript comparison: {type_report}", flush=True)

    run(
        [
            str(source / "node_modules/.bin/esbuild"),
            "src/component/FileManager/TreeView/folderListing.ts",
            "--bundle",
            "--platform=node",
            "--format=esm",
            "--outfile=" + str(workspace / "folder-listing.mjs"),
        ]
    )
    run(
        ["node", str(integration / "test-tree.mjs"), str(workspace / "folder-listing.mjs")],
        log="tree-tests.log",
    )
    print("Directory-tree checks passed; building native frontend", flush=True)
    run(
        [
            str(source / "node_modules/.bin/esbuild"),
            "src/component/Uploader/core/fileDropZone.ts",
            "--bundle",
            "--platform=node",
            "--format=esm",
            "--outfile=" + str(workspace / "file-drop.mjs"),
        ]
    )
    run(["node", str(integration / "test-drop.mjs"), str(workspace / "file-drop.mjs")], log="drop-tests.log")
    run(["npm", "run", "build"], env={**os.environ, "AKB_FRONTEND_REVISION": revision})
    version = json.loads((source / "build/version.json").read_text())
    if version.get("version") != metadata["backend_version"] or version.get("revision") != revision:
        raise RuntimeError("Built frontend version mismatch")
    (workspace / "build-result.json").write_text(
        json.dumps(
            {
                **version,
                "upstream_commit": metadata["upstream_commit"],
                "typecheck": type_report,
                "tree_tests": 8,
                "drop_tests": 4,
            },
            indent=2,
        )
    )
    if args.deploy:
        deployment = activate_assets(source, revision, run)
        (workspace / "deployment.json").write_text(json.dumps(deployment, indent=2))
        print("Local Cloudreve static assets deployed; data and server image preserved", flush=True)
    print(f"Build and checks saved to {workspace.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
