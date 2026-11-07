"""Reparse an unchanged original using the durable, fenced ingestion queue."""

from fastapi import HTTPException
from pydantic import Field

from akb import services
from akb.models import Job
from akb.schemas import Input


class ReparseSource(Input):
    expected_revision: int = Field(ge=1)


def enqueue(session, principal, source_id, expected_revision):
    tenant = services.lock_tenant(session, principal.tenant_id)
    source = services.get_source(session, source_id)
    if source.trashed_at or source.status in {"queued", "processing"}:
        raise HTTPException(409, "资料已在处理或已移入回收站，请读取资料状态")
    if source.revision != expected_revision:
        raise HTTPException(409, "资料版本已变化，请重读后决定是否重新解析")
    source.revision += 1
    source.status = "queued"
    source.error = None
    tenant.revision += 1
    job = Job(tenant_id=principal.tenant_id, source_id=source.id, source_revision=source.revision)
    session.add(job)
    services.audit(session, principal.tenant_id, "source.reparse", source.id)
    session.flush()
    return {
        "source": services.source_dict(source),
        "job_id": job.id,
        "notice": "保留原件及人工知识页；旧引文/填写回执失效，完成后需重读并重新关联出处。",
    }
