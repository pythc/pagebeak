"""File hashes describe state; durable events retain operations even if state later returns to an old value."""

import hashlib
import json

from sqlalchemy import select

from akb.models import FileChange, Job, NativeConnection, Source


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def snapshot(source):
    return {
        "id": source.id,
        "name": source.filename,
        "folder_id": source.folder_id,
        "sha256": source.sha256,
        "size": source.size,
        "deleted": bool(source.revoked_at),
        "origin_path": source.origin_path,
    }


def state_hash(source):
    return digest(snapshot(source))


def record(session, tenant_id, file_id, action, actor, before, after, source_id=None):
    event = FileChange(
        tenant_id=tenant_id,
        file_id=file_id,
        source_id=source_id,
        action=action,
        actor=actor,
        before=before,
        after=after,
    )
    session.add(event)
    return event


def status(session):
    sources = list(session.scalars(select(Source).order_by(Source.id)))
    current = [[s.id, state_hash(s)] for s in sources]
    indexed = [[s.id, s.indexed_hash] for s in sources]
    latest = session.scalar(select(FileChange.id).order_by(FileChange.id.desc()).limit(1)) or 0
    connection = session.scalar(select(NativeConnection))
    pending = session.scalar(
        select(Job.id).where(Job.kind == "native_sync", Job.status.in_(["queued", "running"])).limit(1)
    )
    return {
        "current_hash": digest(current),
        "indexed_hash": digest(indexed),
        "needs_update": current != indexed,
        "latest_change_id": latest,
        "last_scan_at": connection.last_scan_at if connection else None,
        "scan_error": connection.error if connection else None,
        "scan_pending": bool(pending),
        "file_count": len(sources),
        "files_truncated": len(sources) > 100,
        "files": [
            {
                "id": s.id,
                "name": s.filename,
                "current_hash": state_hash(s),
                "indexed_hash": s.indexed_hash,
                "needs_update": state_hash(s) != s.indexed_hash,
                "status": s.status,
                "deleted": bool(s.revoked_at),
            }
            for s in sources[:100]
        ],
    }
