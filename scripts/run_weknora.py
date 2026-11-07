"""Run a pinned private WeKnora instance. No LLM secrets are inherited."""
import argparse
import json
import os
import secrets
from pathlib import Path

from akb.weknora_client import private_write

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--binary", type=Path, required=True)
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--state", type=Path, required=True)
parser.add_argument("--port", type=int, default=5881)
args = parser.parse_args()
state = args.state.resolve()
state.mkdir(parents=True, exist_ok=True, mode=0o700)
keys = state / "runtime-secrets.json"
if not keys.exists():
    private_write(keys, {"JWT_SECRET": secrets.token_hex(32), "SYSTEM_AES_KEY": secrets.token_hex(16)})
private = json.loads(keys.read_text())
empty = state / "empty.env"
empty.touch(mode=0o600, exist_ok=True)
env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ}
env.update(private)
env.update({"WEKNORA_EXTERNAL_EXECUTOR": "workbuddy", "DB_DRIVER": "sqlite", "DB_PATH": str(state / "weknora.db"),
    "RETRIEVE_DRIVER": "sqlite", "STORAGE_TYPE": "local", "LOCAL_STORAGE_BASE_DIR": str(state / "files"),
    "STREAM_MANAGER_TYPE": "memory", "SERVER_HOST": "127.0.0.1", "SERVER_PORT": str(args.port),
    "WEKNORA_SANDBOX_MODE": "disabled", "NEO4J_ENABLE": "false", "ENABLE_GRAPH_RAG": "false",
    "GIN_MODE": "release", "LOG_LEVEL": "error", "CONCURRENCY_POOL_SIZE": "2", "GOMAXPROCS": "2",
    "WEKNORA_TENANT_ENABLE_RBAC": "true", "WEKNORA_TENANT_ENABLE_CROSS_TENANT_ACCESS": "false",
    "ENV_FILE": str(empty), "OLLAMA_BASE_URL": "http://127.0.0.1:1"})
binary = args.binary.resolve()
os.chdir(args.source.resolve())
os.execve(binary, [str(binary)], env)
