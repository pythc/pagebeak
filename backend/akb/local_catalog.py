"""Shared local-document directory metadata. This module never reads local paths."""

from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field, field_validator
from sqlalchemy import exists, select

from akb import services
from akb.models import LocalCatalog, LocalSourceLink, Source, now
from akb.schemas import Input


class CatalogEntry(Input):
    id: UUID
    root_label: str = Field(min_length=1, max_length=200)
    root_id: str = Field(pattern=r"^[a-f0-9]{12}$")
    path: str = Field(min_length=1, max_length=2000)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    status: Literal["indexed", "deferred", "failed", "missing", "offline", "changed"]
    coverage: str | None = Field(default=None, max_length=24)
    warning: str | None = Field(default=None, max_length=500)
    source_id: UUID | None = None
    imported_current: bool = False

    @field_validator("path", "root_label")
    @classmethod
    def relative_only(cls, value):
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value or any(ord(c) < 32 for c in value):
            raise ValueError("共享目录只接受相对名称，不接受绝对路径或控制字符")
        return value


class CatalogSnapshot(Input):
    device_id: UUID
    snapshot_id: UUID
    expected_revision: int = Field(ge=0)
    items: list[CatalogEntry] = Field(max_length=10000)


class MatchSource(Input):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    subject_scope: Literal["self", "reference"]


class SourceObservation(Input):
    document_id: UUID
    source_id: UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    current_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    status: Literal["indexed", "changed", "deferred", "failed", "missing", "offline"]


class Observations(Input):
    device_id: UUID
    items: list[SourceObservation] = Field(max_length=10000)


def usable_sources():
    return ~exists(
        select(LocalSourceLink.id).where(
            LocalSourceLink.source_id == Source.id, LocalSourceLink.stale.is_(True)
        )
    )


def ensure_current(session, source_id):
    if session.scalar(
        select(LocalSourceLink.id)
        .where(LocalSourceLink.source_id == source_id, LocalSourceLink.stale.is_(True))
        .limit(1)
    ):
        raise HTTPException(409, "本机原件已变化，旧收录版本不可用于当前填写；请收录当前版本并复核")


def publish(session, tenant, body):
    services.lock_tenant(session, tenant)
    device = str(body.device_id)
    obj = session.scalar(select(LocalCatalog).where(LocalCatalog.device_id == device))
    if obj and obj.snapshot_id == str(body.snapshot_id):
        if obj.items != [i.model_dump(mode="json") for i in body.items]:
            raise HTTPException(409, "同一清单快照不能重放不同内容")
        return {"revision": obj.revision, "replayed": True}
    if (obj.revision if obj else 0) != body.expected_revision:
        raise HTTPException(409, "本机目录修订号已变化，请重新读取后同步")
    ids = [str(i.id) for i in body.items]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "本机目录包含重复文档 ID")
    if not obj:
        obj = LocalCatalog(tenant_id=tenant, device_id=device, revision=0)
        session.add(obj)
    obj.items = [i.model_dump(mode="json") for i in body.items]
    obj.snapshot_id, obj.updated_at = str(body.snapshot_id), now()
    obj.revision += 1
    session.flush()
    return {"revision": obj.revision, "count": len(obj.items)}


def listing(session, device_id=None, offset=0, limit=50):
    query = select(LocalCatalog).order_by(LocalCatalog.updated_at.desc())
    if device_id:
        query = query.where(LocalCatalog.device_id == str(device_id))
    devices = list(session.scalars(query))
    entries = [
        {**item, "device_id": d.device_id, "observed_at": d.updated_at.isoformat()}
        for d in devices
        for item in d.items
    ]
    sources = {
        s.id: s
        for s in session.scalars(
            select(Source).where(Source.revoked_at.is_(None), Source.trashed_at.is_(None), usable_sources())
        )
    }
    for entry in entries:
        source = sources.get(entry.get("source_id"))
        entry["imported_current"] = bool(source and source.sha256 == entry["sha256"])
    return {
        "items": entries[offset : offset + limit],
        "total": len(entries),
        "has_more": offset + limit < len(entries),
        "revision": devices[0].revision if device_id and devices else 0,
        "devices": [
            {"id": d.device_id, "updated_at": d.updated_at.isoformat(), "count": len(d.items)}
            for d in devices
        ],
        "availability": "最近同步的目录，不代表原件当前在线；阅读正文需当前设备的 WorkBuddy",
    }


def observe(session, tenant, body):
    services.lock_tenant(session, tenant)
    stale_sources = set()
    for item in body.items:
        src = services.get_source(session, str(item.source_id))
        if src.sha256 != item.sha256:
            raise HTTPException(409, "本机绑定摘要与已收录原件不符")
        link = session.scalar(
            select(LocalSourceLink).where(
                LocalSourceLink.device_id == str(body.device_id),
                LocalSourceLink.document_id == str(item.document_id),
                LocalSourceLink.source_id == src.id,
            )
        )
        # Local deletion/offline retains the uploaded snapshot; a content change invalidates it.
        stale = item.status not in {"missing", "offline"} and item.current_hash != item.sha256
        if not link:
            link = LocalSourceLink(
                tenant_id=tenant,
                device_id=str(body.device_id),
                document_id=str(item.document_id),
                source_id=src.id,
                sha256=item.sha256,
                stale=False,
            )
            session.add(link)
        if stale != link.stale:
            src.revision += 1
        link.current_hash, link.stale = item.current_hash, stale
        if stale:
            stale_sources.add(src.id)
    return {"observed": len(body.items), "stale_source_ids": sorted(stale_sources)}
