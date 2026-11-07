import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from akb import mcp_server as bridge
from akb import mcp_upload
from conftest import drain, register
from fastapi.testclient import TestClient
from test_space import quota


def connect(app, monkeypatch, tmp_path, scopes=None):
    application, browser = app
    user = register(browser)
    token = browser.post("/v1/tokens", json={
        "label": "MCP 原件上传测试", "scopes": scopes or ["knowledge:read", "knowledge:write"],
    }).json()["token"]
    kb = browser.get("/v1/context").json()["knowledge_base_id"]
    agent = TestClient(application)
    agent.headers.update({"Authorization": "Bearer " + token, "X-AKB-Knowledge-Base": kb})
    monkeypatch.setenv("AKB_EXPECTED_KB_ID", kb)
    monkeypatch.setenv("AKB_FILE_INBOX_ROOT", str(tmp_path / "inbox"))

    def api(method, path, body=None, key=None, *, content=None):
        headers = {"Idempotency-Key": key} if key else {}
        response = agent.request(method, path, headers=headers,
                                 **({"content": content} if content is not None else {"json": body}))
        if response.status_code >= 300:
            raise bridge.KnowledgeAPIError(str(response.json().get("detail")), response.status_code)
        return response.json()

    monkeypatch.setattr(bridge, "call", api)
    return browser, agent, api, kb, user


def stage(monkeypatch, tmp_path, api, name="原件.txt", data="项目：海棠课堂\n负责人：合成教师".encode()):
    from pathlib import Path

    prepared = mcp_upload.prepare(api)
    path = Path(prepared["staging_directory"]) / name
    path.write_bytes(data)
    return path, data


def test_mcp_original_upload_parse_catalog_replay_and_isolation(app, worker, monkeypatch, tmp_path):
    browser, agent, api, kb, _ = connect(app, monkeypatch, tmp_path)
    path, data = stage(monkeypatch, tmp_path, api)
    key = uuid4().hex
    result = bridge.upload_file(str(path), key)
    assert result["sha256_verified"] and result["source"]["subject_scope"] == "reference"
    assert result["source"]["sha256"] == hashlib.sha256(data).hexdigest()
    source_id = result["source"]["id"]
    assert agent.get(f"/v1/sources/{source_id}/original").content == data
    assert path.read_bytes() == data
    usage = browser.get("/v1/storage").json()
    assert usage["used_bytes"] == len(data) and usage["reserved_bytes"] == 0
    second = bridge.upload_file(str(path), key)
    assert second["source"]["id"] == source_id and second["job_id"] == result["job_id"] and second["replayed"]
    assert browser.get("/v1/storage").json()["used_bytes"] == len(data)
    drain(worker)
    assert bridge.get_job(result["job_id"])["status"] == "completed"
    assert "海棠课堂" in str(bridge.search_knowledge("海棠课堂"))
    assert source_id in str(bridge.read_catalog())
    state = bridge.read_file_upload(result["upload_id"])
    assert state["status"] == "completed" and state["offset"] == len(data)
    assert bridge.cancel_file_upload(result["upload_id"])["status"] == "completed"
    assert agent.get(f"/v1/sources/{source_id}/original").content == data
    # The same request key is scoped to a tenant and cannot reveal the first user's upload.
    with TestClient(app[0]) as other:
        register(other)
        assert other.get(f"/v1/uploads/{result['upload_id']}").status_code == 404
        assert other.delete(f"/v1/uploads/{result['upload_id']}").status_code == 404
        assert other.get(f"/v1/sources/{source_id}/original").status_code == 404
        fresh = other.post("/v1/uploads", json={"filename": path.name, "size": len(data)},
                           headers={"Idempotency-Key": key})
        assert fresh.status_code == 201 and fresh.json()["id"] != result["upload_id"]


def test_lost_chunk_response_and_interrupted_process_resume(app, monkeypatch, tmp_path):
    browser, _, api, _, _ = connect(app, monkeypatch, tmp_path)
    path, data = stage(monkeypatch, tmp_path, api, data=b"A" * 11 + b"B" * 13)
    real_prepare = mcp_upload.prepare
    monkeypatch.setattr(mcp_upload, "prepare", lambda call: {**real_prepare(call), "chunk_bytes": 8})
    key = uuid4().hex
    puts = 0

    def unreliable(method, target, body=None, request_key=None, *, content=None):
        nonlocal puts
        if method == "PUT":
            puts += 1
            if puts == 1:
                api(method, target, body, request_key, content=content)
                raise bridge.KnowledgeAPIError("response lost")
            if puts == 2:
                raise bridge.KnowledgeAPIError("temporary server failure", 503)
        return api(method, target, body, request_key, content=content)

    with pytest.raises(ValueError, match="upload_id="):
        mcp_upload.upload(unreliable, str(path), key)
    assert browser.get("/v1/storage").json()["reserved_bytes"] == len(data)
    resumed = mcp_upload.upload(api, str(path), key)
    assert resumed["sha256_verified"]
    assert browser.get(f"/v1/sources/{resumed['source']['id']}/original").content == data
    assert browser.get("/v1/storage").json()["reserved_bytes"] == 0


def test_request_binding_rejects_changed_contents_and_bad_wire_hash(app, monkeypatch, tmp_path):
    browser, _, api, _, _ = connect(app, monkeypatch, tmp_path)
    path, data = stage(monkeypatch, tmp_path, api, data=b"original")
    key = uuid4().hex

    def offline_chunk(method, target, body=None, request_key=None, *, content=None):
        if method == "PUT":
            raise bridge.KnowledgeAPIError("offline", 503)
        return api(method, target, body, request_key, content=content)

    with pytest.raises(ValueError):
        mcp_upload.upload(offline_chunk, str(path), key)
    path.write_bytes(b"changed!")
    with pytest.raises(ValueError, match="不同文件"):
        mcp_upload.upload(api, str(path), key)
    assert browser.get("/v1/storage").json()["reserved_bytes"] == len(data)
    body = {"filename": "bad.txt", "size": 3, "sha256": hashlib.sha256(b"yes").hexdigest()}
    assert browser.post("/v1/uploads", json=body).status_code == 422
    started = api("POST", "/v1/uploads", body, uuid4().hex)
    api("PUT", f"/v1/uploads/{started['id']}/content?offset=0", content=b"bad")
    with pytest.raises(bridge.KnowledgeAPIError, match="SHA-256"):
        api("POST", f"/v1/uploads/{started['id']}/complete")
    assert browser.get("/v1/sources").json()["items"] == []
    assert bridge.cancel_file_upload(started["id"])["status"] == "cancelled"


def test_rejects_outside_symlink_hardlink_fifo_empty_and_oversized(app, monkeypatch, tmp_path):
    browser, _, api, _, _ = connect(app, monkeypatch, tmp_path)
    path, _ = stage(monkeypatch, tmp_path, api)
    outside = tmp_path / "not-authorized.txt"
    outside.write_text("private")
    with pytest.raises(ValueError, match="收件目录"):
        bridge.upload_file(str(outside), uuid4().hex)
    linked = path.parent / "linked.txt"
    linked.symlink_to(outside)
    with pytest.raises(ValueError, match="链接"):
        bridge.upload_file(str(linked), uuid4().hex)
    linked.unlink()
    os.link(outside, linked)
    with pytest.raises(ValueError, match="硬链接"):
        bridge.upload_file(str(linked), uuid4().hex)
    linked.unlink()
    os.mkfifo(linked)
    with pytest.raises(ValueError, match="普通文件"):
        bridge.upload_file(str(linked), uuid4().hex)
    linked.unlink()
    for size in [0, 2 * 1024**3 + 1]:
        with linked.open("wb") as stream:
            stream.truncate(size)
        with pytest.raises(ValueError, match="2 GiB"):
            bridge.upload_file(str(linked), uuid4().hex)
    linked.unlink()
    linked.mkdir()
    with pytest.raises(ValueError, match="普通文件"):
        bridge.upload_file(str(linked), uuid4().hex)
    assert browser.get("/v1/storage").json()["reserved_bytes"] == 0


def test_read_only_token_and_local_change_do_not_create_false_success(app, monkeypatch, tmp_path):
    browser, _, api, _, _ = connect(app, monkeypatch, tmp_path)
    path, data = stage(monkeypatch, tmp_path, api)

    def changing(method, target, body=None, key=None, *, content=None):
        result = api(method, target, body, key, content=content)
        if method == "PUT":
            path.write_bytes(b"x" * len(data))
        return result

    with pytest.raises(ValueError, match="发生变化"):
        mcp_upload.upload(changing, str(path), uuid4().hex)
    assert browser.get("/v1/storage").json()["reserved_bytes"] == 0
    assert browser.get("/v1/sources").json()["items"] == []
    read_token = browser.post("/v1/tokens", json={"label": "read only", "scopes": ["knowledge:read"]}).json()["token"]
    with TestClient(app[0]) as reader:
        response = reader.post("/v1/uploads", json={"filename": "no.txt", "size": 2},
                               headers={"Authorization": "Bearer " + read_token})
        assert response.status_code == 403


def test_concurrent_begin_is_idempotent_and_mcp_obeys_quota(app, monkeypatch, tmp_path):
    browser, _, api, _, user = connect(app, monkeypatch, tmp_path)
    key = uuid4().hex
    body = {"filename": "same.txt", "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}
    with ThreadPoolExecutor(max_workers=3) as pool:
        starts = list(pool.map(lambda _: api("POST", "/v1/uploads", body, key), range(3)))
    assert len({s["id"] for s in starts}) == 1
    assert browser.get("/v1/storage").json()["reserved_bytes"] == 3
    assert bridge.cancel_file_upload(starts[0]["id"])["status"] == "cancelled"
    assert browser.get("/v1/storage").json()["reserved_bytes"] == 0
    path, data = stage(monkeypatch, tmp_path, api)
    quota(app[0], browser, user["id"], len(data) - 1)
    with pytest.raises(bridge.KnowledgeAPIError, match="空间不足"):
        bridge.upload_file(str(path), uuid4().hex)
    assert browser.get("/v1/storage").json()["reserved_bytes"] == 0
