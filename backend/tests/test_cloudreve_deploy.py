import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from akb.config import ROOT

spec = importlib.util.spec_from_file_location("cloudreve_build", ROOT / "scripts/build_cloudreve_frontend.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Runner:
    def __init__(self, failure=None, previous=True):
        self.calls = []
        self.failure = failure
        self.previous = previous
        self.failed = False

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if self.failure and self.failure(command) and not self.failed:
            self.failed = True
            raise RuntimeError("synthetic command failure")
        return SimpleNamespace(returncode=0 if self.previous or "test" not in command else 1)


def test_activation_keeps_a_backup_and_only_restarts_cloudreve():
    runner = Runner()
    result = module.activate_assets(Path("/synthetic"), "test", runner)
    assert result["previous_statics"].startswith("/cloudreve/data/statics.backup-")
    assert runner.calls[-1] == ["docker", "compose", "--profile", "cloudreve", "restart", "cloudreve"]
    assert not any("postgres" in call or "rm" in call for call in runner.calls)


def test_failed_switch_restores_previous_directory():
    runner = Runner(lambda c: c[-2:] == ["/cloudreve/data/statics-stage-test", "/cloudreve/data/statics"])
    with pytest.raises(RuntimeError):
        module.activate_assets(Path("/synthetic"), "test", runner)
    moves = [c for c in runner.calls if "mv" in c]
    assert moves[-1][-1] == "/cloudreve/data/statics"
    assert moves[-1][-2].startswith("/cloudreve/data/statics.backup-")


def test_failed_restart_can_return_to_embedded_frontend():
    runner = Runner(lambda c: "restart" in c, previous=False)
    with pytest.raises(RuntimeError):
        module.activate_assets(Path("/synthetic"), "test", runner)
    assert any(c[-1] == "/cloudreve/data/statics-stage-test-failed" for c in runner.calls)
    assert sum("restart" in c for c in runner.calls) == 2


def test_copy_failure_never_replaces_active_assets():
    runner = Runner(lambda c: c[1] == "cp")
    with pytest.raises(RuntimeError):
        module.activate_assets(Path("/synthetic"), "test", runner)
    assert not any("mv" in c or "restart" in c for c in runner.calls)
