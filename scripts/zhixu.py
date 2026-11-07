"""Local service lifecycle. Never signal a process without its saved identity."""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

import httpx
from akb.config import ROOT, Settings
from akb.schema_version import check_schema

RUN = ROOT / "data/runtime"
STATE = RUN / "service.json"
COMMAND = [str(ROOT / ".venv/bin/python"), "scripts/dev.py"]


def identity(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def owned(state):
    if not state or not isinstance(state.get("pid"), int) or not state.get("identity"):
        return False
    return identity(state["pid"]) == state["identity"] and "scripts/dev.py" in state["identity"]


def read_state():
    try:
        state = json.loads(STATE.read_text())
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("运行状态文件损坏，已保留原文件，请人工核对") from exc
    if (not isinstance(state, dict) or not isinstance(state.get("pid"), int)
            or state["pid"] <= 0 or not isinstance(state.get("identity"), str)):
        raise RuntimeError("运行状态格式不正确，已保留原文件，请人工核对")
    return state


def health():
    settings = Settings()
    endpoints = {
        "knowledge": f"http://127.0.0.1:{settings.api_port}/healthz",
        "files": settings.native_cloudreve_url + "/version.json",
        "memory_core": settings.memory_core_url + "/health",
        "memory_wiki": settings.memory_wiki_url + "/health",
    }
    result = {}
    with httpx.Client(trust_env=False, timeout=2, follow_redirects=False) as client:
        for name, url in endpoints.items():
            try:
                response = client.get(url)
                result[name] = response.status_code == 200
            except httpx.HTTPError:
                result[name] = False
    return result


def stop():
    state = read_state()
    if not state:
        return
    if not owned(state):
        if identity(state.get("pid", 0)):
            raise RuntimeError("进程身份已变化，已停止操作；请人工核对，不会结束其他进程")
        STATE.unlink(missing_ok=True)
        return
    os.kill(state["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 12
    while owned(state) and time.monotonic() < deadline:
        time.sleep(0.2)
    if owned(state):
        raise RuntimeError("服务没有正常退出，请检查日志；没有强行结束进程")
    STATE.unlink(missing_ok=True)


def prepare_infrastructure():
    deadline = time.monotonic() + 45
    while True:
        try:
            result = subprocess.run(
                ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=5,
            )
            if result.returncode == 0:
                break
        except subprocess.TimeoutExpired:
            pass  # Docker Desktop may still be bringing up its socket.
        if time.monotonic() >= deadline:
            raise RuntimeError("Docker 尚未就绪，请先打开 Docker Desktop 后重试")
        time.sleep(1)
    # PostgreSQL has a real health check. 'Started' alone does not mean it can
    # accept connections; starting the worker before that used to kill the group.
    subprocess.run(
        ["docker", "compose", "--profile", "cloudreve", "up", "-d", "--wait", "--wait-timeout", "60"],
        cwd=ROOT, check=True, timeout=75,
    )
    check_schema()


def start():
    state = read_state()
    if owned(state):
        if all(health().values()):
            check_schema()
            return
        prepare_infrastructure()
        if owned(state):
            if all(health().values()):
                return
            raise RuntimeError("已管理的服务未完全就绪，请先查看状态与日志，再执行 restart")
    existing = health()
    if any(existing[k] for k in ("knowledge", "memory_core", "memory_wiki")):
        raise RuntimeError("检测到未由此入口管理的知识服务；请核对并正常停止原启动器后重试")
    prepare_infrastructure()
    with (RUN / "service.log").open("a") as log:
        os.chmod(RUN / "service.log", 0o600)
        process = subprocess.Popen(
            COMMAND, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    state = {"pid": process.pid, "identity": identity(process.pid)}
    if not state["identity"]:
        process.terminate()
        raise RuntimeError("启动器未能保持运行，请查看 data/runtime/service.log")
    temporary = RUN / "service.json.tmp"
    try:
        temporary.write_text(json.dumps(state))
        temporary.chmod(0o600)
        temporary.replace(STATE)
    except OSError:
        process.terminate()
        process.wait(timeout=12)
        raise
    deadline = time.monotonic() + 45
    while process.poll() is None and time.monotonic() < deadline:
        if all(health().values()):
            return
        time.sleep(0.5)
    stop()
    raise RuntimeError("启动未就绪，已正常回收本次进程；请查看 data/runtime/service.log")


def main():
    parser = argparse.ArgumentParser(description="知序本机服务：start / status / stop / restart")
    parser.add_argument("action", choices=["start", "status", "stop", "restart"])
    args = parser.parse_args()
    RUN.mkdir(parents=True, exist_ok=True, mode=0o700)
    if RUN.is_symlink() or STATE.is_symlink():
        raise RuntimeError("运行状态路径不能是符号链接")
    with (RUN / "service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action in {"stop", "restart"}:
            stop()
        if args.action in {"start", "restart"}:
            start()
        checks = health()
        print(json.dumps({"managed": owned(read_state()), "health": checks,
                          "files": "http://localhost:5878/", "start": "http://localhost:5878/start",
                          "guide": "http://localhost:5878/zhixu-guide.html"}, ensure_ascii=False, indent=2))
        if args.action != "stop" and (not owned(read_state()) or not all(checks.values())):
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"知序未完成操作：{exc}。若 Docker 未运行，请先打开 Docker Desktop。", file=sys.stderr)
        sys.exit(1)
