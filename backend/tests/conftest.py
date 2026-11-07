import os
import subprocess
from uuid import uuid4

import pytest
from akb.api import create_app
from akb.config import ROOT, Settings
from akb.worker import Worker
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url


@pytest.fixture(scope="session")
def test_settings():
    current = Settings()
    urls = {}
    for key in ("database_url", "worker_database_url", "migration_database_url"):
        urls[key] = (
            make_url(getattr(current, key)).set(database="akb_test").render_as_string(hide_password=False)
        )
    assert make_url(urls["database_url"]).database == "akb_test"
    env = {**os.environ, "AKB_MIGRATION_DATABASE_URL": urls["migration_database_url"]}
    subprocess.run(
        [str(ROOT / ".venv/bin/alembic"), "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    return current.model_copy(update={**urls, "knowledge_engine": "legacy", "cloudreve_credentials": None, "native_sync_enabled": False})


@pytest.fixture
def app(test_settings, tmp_path, monkeypatch):
    # Installer subprocess helpers instantiate Settings independently; keep legacy
    # fixtures isolated from the developer's active Tencent environment as well.
    monkeypatch.setenv("AKB_KNOWLEDGE_ENGINE", "legacy")
    settings = test_settings.model_copy(update={"storage_dir": tmp_path, "native_secrets_dir": tmp_path / "native-sessions",
                                                "workbuddy_package_dir": tmp_path / "workbuddy-packages"})
    application = create_app(settings)
    with TestClient(application) as client:
        yield application, client


@pytest.fixture
def worker(app):
    w = Worker(app[0].state.settings)
    # Prior tests may have intentionally left a queued operation.
    # A forward migration can enqueue a refresh for every persisted test tenant.
    for _ in range(3000):
        if not w.run_once():
            break
    else:
        raise AssertionError("Pre-existing migration/backfill queue did not settle")
    yield w
    w.close()


def register(client, name="测试用户"):
    result = client.post(
        "/v1/auth/register",
        json={"email": f"{uuid4().hex}@example.test", "password": "test-password-2026", "display_name": name},
    )
    assert result.status_code == 201, result.text
    data = result.json()
    client.headers["X-CSRF-Token"] = data["csrf"]
    return data


def upload(
    client, text="姓名：陈舟\n工作单位：示例大学\n职称：讲师\n邮箱：chen@example.test", scope="self", key=None
):
    response = client.post(
        "/v1/sources/upload",
        files={"file": ("简历.txt", text.encode(), "text/plain")},
        data={"subject_scope": scope},
        headers={"Idempotency-Key": key or uuid4().hex},
    )
    assert response.status_code == 202, response.text
    return response.json()


def drain(worker):
    for _ in range(150):
        if not worker.run_once():
            return
    raise AssertionError("Queue did not settle")
