import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/zhixu.py"
    spec = importlib.util.spec_from_file_location("local_lifecycle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(module, "RUN", tmp_path)
    monkeypatch.setattr(module, "check_schema", lambda: None)
    return module


def test_reused_pid_is_never_signalled(lifecycle, monkeypatch):
    m = lifecycle
    m.STATE.write_text('{"pid":123,"identity":"old process scripts/dev.py"}')
    monkeypatch.setattr(m, "identity", lambda _: "new unrelated process")
    monkeypatch.setattr(m.os, "kill", lambda *_: pytest.fail("Unrelated process signalled"))
    with pytest.raises(RuntimeError, match="身份已变化"):
        m.stop()
    assert m.STATE.exists()


def test_second_start_does_not_duplicate_process(lifecycle, monkeypatch):
    m = lifecycle
    m.STATE.write_text('{"pid":123,"identity":"same scripts/dev.py"}')
    monkeypatch.setattr(m, "identity", lambda _: "same scripts/dev.py")
    monkeypatch.setattr(m, "health", lambda: {"knowledge": True, "files": True})
    monkeypatch.setattr(m.subprocess, "Popen", lambda *_a, **_k: pytest.fail("Duplicate process"))
    m.start()


def test_foreign_runtime_is_not_adopted_or_replaced(lifecycle, monkeypatch):
    m = lifecycle
    monkeypatch.setattr(m, "health", lambda: {
        "knowledge": False, "memory_core": True, "memory_wiki": False, "files": True,
    })
    monkeypatch.setattr(m.subprocess, "run", lambda *_a, **_k: pytest.fail("Foreign runtime touched"))
    with pytest.raises(RuntimeError, match="未由此入口管理"):
        m.start()


def test_dead_process_state_is_recoverable(lifecycle, monkeypatch):
    m = lifecycle
    m.STATE.write_text('{"pid":123,"identity":"old scripts/dev.py"}')
    monkeypatch.setattr(m, "identity", lambda _: "")
    monkeypatch.setattr(m.os, "kill", lambda *_: pytest.fail("Dead PID signalled"))
    m.stop()
    assert not m.STATE.exists()


def test_state_publish_failure_cleans_up_new_supervisor(lifecycle, monkeypatch):
    from types import SimpleNamespace

    m = lifecycle
    calls = []
    process = SimpleNamespace(pid=123, terminate=lambda: calls.append("terminate"),
                              wait=lambda **_kw: calls.append("wait"))
    monkeypatch.setattr(m, "health", lambda: {"knowledge": False, "memory_core": False, "memory_wiki": False})
    monkeypatch.setattr(m, "identity", lambda _: "new scripts/dev.py")
    monkeypatch.setattr(m, "prepare_infrastructure", lambda: None)
    monkeypatch.setattr(m.subprocess, "run", lambda *_a, **_kw: None)
    monkeypatch.setattr(m.subprocess, "Popen", lambda *_a, **_kw: process)
    original = Path.write_text

    def fail_state(path, *args, **kwargs):
        if path.name == "service.json.tmp":
            raise PermissionError("state write failed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_state)
    with pytest.raises(PermissionError):
        m.start()
    assert calls == ["terminate", "wait"]


def test_database_health_failure_prevents_application_start(lifecycle, monkeypatch):
    m = lifecycle
    monkeypatch.setattr(m, "health", lambda: {"knowledge": False, "memory_core": False, "memory_wiki": False})
    monkeypatch.setattr(m.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("Started before database ready"))
    commands = []

    def fail_wait(command, **kwargs):
        from types import SimpleNamespace

        commands.append(command)
        if "compose" in command:
            raise m.subprocess.CalledProcessError(1, command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(m.subprocess, "run", fail_wait)
    with pytest.raises(m.subprocess.CalledProcessError):
        m.start()
    assert "--wait" in commands[-1] and "--wait-timeout" in commands[-1]


def test_docker_boot_is_allowed_to_finish(lifecycle, monkeypatch):
    from types import SimpleNamespace

    m = lifecycle
    readiness = iter([1, 1, 0])
    commands, waits = [], []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=next(readiness) if command == ["docker", "info"] else 0)

    monkeypatch.setattr(m.subprocess, "run", run)
    monkeypatch.setattr(m.time, "sleep", waits.append)
    m.prepare_infrastructure()
    assert waits == [1, 1]
    assert commands[-1][-3:] == ["--wait", "--wait-timeout", "60"]


def test_docker_info_timeout_is_retried(lifecycle, monkeypatch):
    from types import SimpleNamespace

    m = lifecycle
    calls, waits = [], []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise m.subprocess.TimeoutExpired(command, 5)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(m.subprocess, "run", run)
    monkeypatch.setattr(m.time, "sleep", waits.append)
    m.prepare_infrastructure()
    assert waits == [1] and "--wait" in calls[-1]


def test_schema_mismatch_prevents_app_launch(lifecycle, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(lifecycle, "health", lambda: {"knowledge":False,"memory_core":False,"memory_wiki":False})
    monkeypatch.setattr(lifecycle.subprocess, "run", lambda *_a,**_k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(lifecycle.subprocess, "Popen", lambda *_a,**_k: pytest.fail("Launched with wrong schema"))

    def mismatch():
        raise RuntimeError("数据库版本与本版程序不符")

    monkeypatch.setattr(lifecycle, "check_schema", mismatch)
    with pytest.raises(RuntimeError,match="数据库版本"):
        lifecycle.start()
