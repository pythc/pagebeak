"""Run the built web application, API and worker locally. Ctrl-C stops these child processes."""

import signal
import subprocess
import sys
import time

import httpx
from akb.config import ROOT, Settings
from akb.schema_version import check_schema
from akb.tencent_runtime import node_executable

check_schema()

if Settings().knowledge_engine == "legacy" and not (ROOT / "web/dist/index.html").exists():
    raise SystemExit("Build the frontend first: npm run build --prefix web")

children = []


def stop(_signal=None, _frame=None):
    for child in children:
        if child.poll() is None:
            child.terminate()


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
try:
    if Settings().knowledge_engine == "weknora":
        from akb.weknora_client import WeKnoraClient

        try:
            WeKnoraClient(Settings(), "00000000-0000-0000-0000-000000000000").mode()
        except Exception as exc:
            raise SystemExit("Start scripts/run_weknora.py first; external-executor mode must be healthy before API startup") from exc
    if Settings().knowledge_engine == "tencent":
        def healthy(port):
            try:
                httpx.get(f"http://127.0.0.1:{port}/health", trust_env=False, timeout=2).raise_for_status()
                return True
            except httpx.HTTPError:
                return False

        ready = [healthy(port) for port in (8420, 8421)]
        if any(ready) and not all(ready):
            raise SystemExit("Only one Tencent service is healthy; repair the existing pair before starting the API")
        if not all(ready):
            node = node_executable()
            if not node.exists():
                raise SystemExit("Prepare Tencent runtime with scripts/setup_tencent_memory.py first") from None
            children.append(subprocess.Popen([sys.executable, "scripts/run_tencent_memory.py", "--node", str(node)], cwd=ROOT))
            deadline = time.monotonic() + 25
            while not all(healthy(port) for port in (8420, 8421)):
                if children[-1].poll() is not None or time.monotonic() >= deadline:
                    raise SystemExit("Tencent services did not become healthy; the API was not started")
                time.sleep(0.3)
    for module in ["akb.api", "akb.worker"]:
        entry = "run" if module.endswith("api") else "main"
        children.append(
            subprocess.Popen([sys.executable, "-c", f"from {module} import {entry}; {entry}()"], cwd=ROOT)
        )
    print(f"Local knowledge workspace: http://localhost:{Settings().api_port}/", flush=True)
    while all(child.poll() is None for child in children):
        time.sleep(0.3)
finally:
    stop()
    for child in children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
