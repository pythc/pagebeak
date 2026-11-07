"""Start pinned, loopback-only Tencent services; Ctrl-C stops both, data is retained."""
import argparse
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path

from akb.config import ROOT

SHA = "6a4bb55ea54834d8265ce52024d3e3c75db30d38"


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    upstream = ROOT / "work/tencent-memory" / ("TencentDB-Agent-Memory-" + SHA)
    key_file = ROOT / "secrets/tencent-memory.key"
    if not key_file.exists():
        key_file.parent.mkdir(exist_ok=True, mode=0o700)
        descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as out:
            out.write(secrets.token_urlsafe(48))
    if key_file.stat().st_mode & 0o077:
        raise SystemExit("Service key must have mode 0600")
    data = ROOT / "data/tencent-memory"
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    data.chmod(0o700)
    (data / "knowledge").mkdir(parents=True, exist_ok=True)
    key = key_file.read_text().strip()
    # This deployment uses only WorkBuddy as the semantic executor. Do not inherit
    # unrelated model credentials or third-party telemetry from the launch shell.
    inherited = {k: v for k, v in os.environ.items() if not any(
        marker in k.upper() for marker in ("LLM", "OPENAI", "ANTHROPIC", "LANGFUSE", "OPIK", "OTEL"))}
    common = {**inherited, "TENCENT_MEMORY_UPSTREAM": str(upstream), "OTEL_SDK_DISABLED": "true",
              "LOG_PATH": str(data / "logs")}
    # These are service keys, never WorkBuddy/model credentials.
    core_env = {**common, "TDAI_GATEWAY_API_KEY": key, "TDAI_DATA_DIR": str(data / "core"),
                "TDAI_GATEWAY_HOST": "127.0.0.1", "TDAI_GATEWAY_PORT": "8420"}
    wiki_env = {**common, "KNOWLEDGE_SERVICE_KEY": key, "PORT": "8421",
                "KNOWLEDGE_DATA_DIR": str(data / "knowledge"),
                "KNOWLEDGE_DB_PATH": str(data / "knowledge/knowledge.db"), "LLM_MODE": "proxy",
                "KNOWLEDGE_AUTO_SYNC_ENABLED": "false"}
    children = []

    def stop(*_):
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        for folder, entry, env in [
            ("MemoryCore", ROOT / "integrations/tencent-memory/core-entry.mts", core_env),
            ("MemoryKnowledge", ROOT / "integrations/tencent-memory/knowledge-entry.mts", wiki_env),
        ]:
            cwd = upstream / folder
            loader = Path(cwd / "node_modules/tsx/dist/loader.mjs")
            children.append(subprocess.Popen([args.node, "--import", str(loader), str(entry)], cwd=cwd, env=env))
        while all(child.poll() is None for child in children):
            time.sleep(0.3)
        if any(child.returncode not in {None, 0, -15} for child in children):
            raise SystemExit("Tencent service exited; inspect local service log")
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
