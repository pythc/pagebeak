"""Reconcile native inventory against committed publication metadata under tenant lock."""

import json
import os
import re
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from akb import services
from akb.library_store import StorageClient
from akb.models import MemoryBinding, MemoryGarbage, MemoryObject

REF = re.compile(r"^zhixu-[a-f0-9-]{36}-[0-9]+-[a-f0-9]{12}\.md$")


def journal_root(settings):
    root = settings.object_root() / ".memory-journals"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def journal(session, settings, tenant, wiki_id, kind, remote):
    directory = journal_root(settings) / str(UUID(tenant))
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (uuid4().hex + ".json")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump({"wiki_id": wiki_id, "kind": kind, "remote": remote}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    session.info.setdefault("published_journals", []).append(path)


def pending_tenants(settings):
    result = []
    for path in journal_root(settings).iterdir():
        try:
            tenant = str(UUID(path.name))
            if path.is_dir() and any(path.glob("*.json")):
                result.append(tenant)
        except ValueError:
            continue
    return result


def inventory(session, settings, tenant):
    from akb.memory_library import checked_binding

    state = checked_binding(session, settings.knowledge_engine)
    client = StorageClient(settings, tenant)
    core = client.call("core", "/v3/zhixu/inventory")
    wiki = (
        client.call("wiki", "/v3/zhixu/inventory", {"wiki_id": state.wiki_id})
        if state
        else {"pages": [], "raw": [], "bytes": 0}
    )
    return core, wiki


def measure(session, settings, tenant):
    owner = services.lock_tenant(session, tenant)
    core, wiki = inventory(session, settings, tenant)
    owner.memory_bytes = core["bytes"] + wiki["bytes"]
    return owner


def reserve(session, settings, tenant, added_bytes):
    owner = measure(session, settings, tenant)
    if owner.used_bytes + owner.reserved_bytes + owner.memory_bytes + added_bytes > owner.quota_bytes:
        raise HTTPException(413, "原文件和知识库派生内容已接近配额，无法发布；请先清理或扩容")
    # Logical UTF-8 payload accounting; native SQLite/index/WAL overhead is not user payload.
    owner.memory_bytes += added_bytes


def reconcile(session, settings, tenant):
    services.lock_tenant(session, tenant)
    client = StorageClient(settings, tenant)
    state = session.scalar(select(MemoryBinding))
    objects = list(session.scalars(select(MemoryObject).where(MemoryObject.deleted.is_(False))))
    live = {obj.remote_ref for obj in objects}
    directory = journal_root(settings) / str(UUID(tenant))
    cleaned = 0
    if directory.exists():
        for path in sorted(directory.glob("*.json"))[:100]:
            info = json.loads(path.read_text())
            ref = info["remote"]
            raw = info["kind"] == "raw"
            if not (
                re.fullmatch(r"[a-f0-9-]{36}-[a-f0-9]{12}-[0-9]{4,}\.md", ref) if raw else REF.fullmatch(ref)
            ):
                raise HTTPException(503, "记忆写入日志格式异常，已停止清理")
            raw_live = (
                {name for item in state.exports.values() for name in item["files"]}
                | {name for obj in objects for name in obj.raw_refs}
                if state and state.wiki_id == info["wiki_id"]
                else set()
            )
            if ref not in (raw_live if raw else live):
                if raw:
                    client.call("wiki", "/v3/wiki/raw/rm", {"wiki_id": info["wiki_id"], "filenames": [ref]})
                elif info["kind"] == "page":
                    client.call("wiki", "/v3/zhixu/forget", {"wiki_id": info["wiki_id"], "refs": [ref]})
                else:
                    client.call("core", "/v3/conversation/delete", {"session_ids": [ref.removesuffix(".md")]})
                cleaned += 1
            # Only remove journal after transaction commits; retries are idempotent.
            session.info.setdefault("published_journals", []).append(path)
    if state:
        core, wiki = inventory(session, settings, tenant)
        pages = [p["ref"] for p in wiki["pages"] if p["ref"] not in live]
        sessions = [s for s in core["sessions"] if REF.fullmatch(s + ".md") and s + ".md" not in live]
        raw_live = {name for item in state.exports.values() for name in item["files"]} | {ref for obj in objects for ref in obj.raw_refs}
        raw = [p["ref"] for p in wiki["raw"] if p["ref"] not in raw_live]
        for ref in pages[:30]:
            client.call("wiki", "/v3/zhixu/forget", {"wiki_id": state.wiki_id, "refs": [ref]})
        if sessions[:30]:
            client.call("core", "/v3/conversation/delete", {"session_ids": sessions[:30]})
        if raw[:30]:
            client.call("wiki", "/v3/wiki/raw/rm", {"wiki_id": state.wiki_id, "filenames": raw[:30]})
        cleaned += len(pages[:30]) + len(sessions[:30]) + len(raw[:30])
    measure(session, settings, tenant)
    return {"removed_orphans": cleaned}


def health(session, settings, tenant):
    from akb.memory_library import library

    owner = measure(session, settings, tenant)
    state = library(session, tenant, limit=1, engine=settings.knowledge_engine)
    return {
        "core": "reachable",
        "wiki": "reachable" if session.scalar(select(MemoryBinding)) else "not_initialized",
        "memory_payload_bytes": owner.memory_bytes,
        "purge_pending": session.scalar(select(func.count()).select_from(MemoryGarbage)),
        "pending_organization": state["pending"],
        "model_executor": "workbuddy",
        "engine": settings.knowledge_engine,
        "ocr": "rapidocr_onnxruntime_cpu",
        "background_model_api": False,
        "accounting": "originals + native raw exports + generated UTF-8 payload; SQLite/index/WAL overhead excluded",
    }
