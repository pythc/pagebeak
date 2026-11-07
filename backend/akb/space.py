"""Tenant-locked logical quotas and per-plan, browser-authorized destructive actions."""

import hashlib
import json
import os
import shutil
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select

from akb import services
from akb.models import Catalog, CatalogEntry, CleanupPlan, Idempotency, Source, StorageArchive, Upload, now


def staging_path(settings, upload):
    return services.object_path(settings, f"{upload.tenant_id}/staging/{upload.id}")


def expire_uploads(session, settings, tenant):
    for upload in session.scalars(
        select(Upload).where(Upload.status == "staging", Upload.expires_at < now())
    ):
        staging_path(settings, upload).unlink(missing_ok=True)
        tenant.reserved_bytes -= upload.size
        upload.status = "expired"
    for upload in session.scalars(
        select(Upload).where(Upload.status == "completed", Upload.expires_at < now())
    ):
        staging_path(settings, upload).unlink(missing_ok=True)
        upload.status = "settled"


def usage(session, settings, tenant_id):
    tenant = services.lock_tenant(session, tenant_id)
    expire_uploads(session, settings, tenant)
    if settings.knowledge_engine in {"tencent", "weknora"}:
        from akb.memory_runtime import measure

        measure(session, settings, tenant_id)
    available = max(0, tenant.quota_bytes - tenant.used_bytes - tenant.reserved_bytes - tenant.memory_bytes)
    ratio = (tenant.used_bytes + tenant.reserved_bytes + tenant.memory_bytes) / tenant.quota_bytes if tenant.quota_bytes else 1
    return {
        "quota_bytes": tenant.quota_bytes,
        "used_bytes": tenant.used_bytes + tenant.memory_bytes,
        "original_bytes": tenant.used_bytes,
        "memory_bytes": tenant.memory_bytes,
        "reserved_bytes": tenant.reserved_bytes,
        "available_bytes": available,
        "pending_deletion_bytes": session.scalar(
            select(func.coalesce(func.sum(Source.size), 0)).where(
                Source.revoked_at.is_not(None), Source.trashed_at.is_(None), Source.object_key != ""
            )
        ),
        "trash_bytes": session.scalar(
            select(func.coalesce(func.sum(Source.size), 0)).where(Source.trashed_at.is_not(None))
        ),
        "usage_ratio": round(ratio, 4),
        "near_full": ratio >= 0.8,
        "status": "full"
        if available == 0
        else "critical"
        if ratio >= 0.95
        else "warning"
        if ratio >= 0.8
        else "normal",
        "upload_limit_bytes": settings.upload_limit,
        "chunk_bytes": settings.upload_chunk_bytes,
        "accounting": "original_files_including_pending_deletion; uploads_reserve_full_size; native_memory_payload_included; SQLite_index_WAL_overhead_excluded",
        "cleanup_requires": "browser_approval_of_exact_plan",
        "suggested_action": "查看清理候选；用户批准后删除或只保留摘要，或联系管理员扩容"
        if ratio >= 0.8
        else None,
    }


def upload_fingerprint(value, sha256):
    return hashlib.sha256(json.dumps([
        value.filename, value.media_type, value.size, value.subject_scope, value.folder_id, sha256,
    ], ensure_ascii=False).encode()).hexdigest()


def begin_upload(session, settings, tenant_id, body, key=None):
    from akb.files_api import folder

    tenant = services.lock_tenant(session, tenant_id)
    expire_uploads(session, settings, tenant)
    if body.sha256 and not key:
        raise HTTPException(422, "指定文件摘要时必须提供 Idempotency-Key，以便校验和恢复上传")
    fingerprint = upload_fingerprint(body, body.sha256)
    if key:
        previous = session.scalar(select(Idempotency).where(
            Idempotency.operation == "chunk_upload", Idempotency.key == key))
        if previous:
            if previous.request_hash != fingerprint:
                raise HTTPException(409, "同一上传请求对应了不同文件或参数，请使用新的请求标识")
            return get_upload(session, previous.resource_id)
    folder(session, body.folder_id)
    if body.size > settings.upload_limit:
        raise HTTPException(413, "单文件超过 2 GiB 上限")
    if tenant.used_bytes + tenant.reserved_bytes + tenant.memory_bytes + body.size > tenant.quota_bytes:
        raise HTTPException(413, "个人空间不足，请释放空间或联系管理员扩容")
    root = settings.object_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(root).free < body.size + settings.disk_reserve_bytes:
        raise HTTPException(507, "服务器磁盘不足，请联系管理员")
    upload = Upload(
        tenant_id=tenant_id,
        filename=body.filename,
        media_type=body.media_type,
        subject_scope=body.subject_scope,
        size=body.size,
        folder_id=body.folder_id,
        expires_at=now() + timedelta(hours=24),
    )
    session.add(upload)
    session.flush()
    if key:
        session.add(Idempotency(tenant_id=tenant_id, operation="chunk_upload", key=key,
                                request_hash=fingerprint, resource_id=upload.id))
    tenant.reserved_bytes += body.size
    return upload


def get_upload(session, upload_id):
    upload = session.get(Upload, upload_id)
    if not upload:
        raise HTTPException(404, "上传会话不存在")
    return upload


def upload_dict(upload):
    return {k: getattr(upload, k) for k in ("id", "size", "offset", "status", "source_id", "expires_at")}


def append_chunk(session, settings, tenant_id, upload_id, offset, data):
    services.lock_tenant(session, tenant_id)
    upload = get_upload(session, upload_id)
    if upload.status != "staging" or upload.expires_at < now():
        raise HTTPException(409, "上传会话已结束或过期")
    if offset != upload.offset:
        raise HTTPException(409, "上传位置不一致，请查询上传会话后续传")
    if not data or len(data) > settings.upload_chunk_bytes or offset + len(data) > upload.size:
        raise HTTPException(413, "分块大小或文件总长度不合法")
    path = staging_path(settings, upload)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(path.parent).free < len(data) + settings.disk_reserve_bytes:
        raise HTTPException(507, "服务器磁盘不足")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "r+b") as stream:
            # A process crash can leave uncommitted trailing bytes. Retry at the database offset.
            stream.truncate(offset)
            stream.seek(offset)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise HTTPException(507, "文件写入失败，上传位置未推进") from exc
    upload.offset += len(data)
    return upload_dict(upload)


def finish_upload(session, settings, tenant_id, upload_id):
    from akb.files_api import folder

    tenant = services.lock_tenant(session, tenant_id)
    upload = get_upload(session, upload_id)
    if upload.status in {"completed", "settled"}:
        source = services.get_source(session, upload.source_id)
        job = session.scalar(
            select(services.Job).where(services.Job.source_id == source.id, services.Job.kind == "ingest")
        )
        return {"source": services.source_dict(source), "job_id": job.id, "replayed": True}
    if upload.status != "staging" or upload.expires_at < now() or upload.offset != upload.size:
        raise HTTPException(409, "上传未完成、已取消或已过期")
    folder(session, upload.folder_id)
    path = staging_path(settings, upload)
    if not path.exists() or path.stat().st_size != upload.size:
        raise HTTPException(409, "暂存文件长度不一致，请重新上传")
    binding = session.scalar(select(Idempotency).where(
        Idempotency.operation == "chunk_upload", Idempotency.resource_id == upload.id))
    if binding and binding.request_hash != upload_fingerprint(upload, None):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while data := stream.read(1024 * 1024):
                digest.update(data)
        if binding.request_hash != upload_fingerprint(upload, digest.hexdigest()):
            raise HTTPException(409, "原文件 SHA-256 不一致，未入库；请检查文件或取消后重新上传")
    tenant.reserved_bytes -= upload.size
    source, job, replayed = services.register_source(
        session,
        settings,
        tenant_id,
        upload.filename,
        upload.media_type,
        None,
        upload.subject_scope,
        staged=path,
        source_id=upload.id,
    )
    upload.status, upload.source_id = "completed", source.id
    source.folder_id = upload.folder_id
    return {"source": services.source_dict(source), "job_id": job.id, "replayed": replayed}


def cancel_upload(session, settings, tenant_id, upload_id):
    tenant = services.lock_tenant(session, tenant_id)
    upload = get_upload(session, upload_id)
    if upload.status == "staging":
        staging_path(settings, upload).unlink(missing_ok=True)
        tenant.reserved_bytes -= upload.size
        upload.status = "cancelled"
    return upload_dict(upload)


def candidates(session):
    rows = list(
        session.scalars(
            select(Source)
            .where(Source.revoked_at.is_(None))
            .order_by(Source.size.desc(), Source.id)
            .limit(50)
        )
    )
    return [
        {**services.source_dict(s), "reason": "较大文件，请判断是否仍有保留价值；大小不代表无用"}
        for s in rows
    ]


def create_plan(session, tenant_id, body):
    services.lock_tenant(session, tenant_id)
    ids = list(dict.fromkeys(body.source_ids))
    agent_summaries = {s.source_id: s for s in body.summaries}
    if (
        len(agent_summaries) != len(body.summaries)
        or not set(agent_summaries).issubset(ids)
        or (body.summaries and body.mode != "summarize")
    ):
        raise HTTPException(422, "归纳总结必须对应清单中的唯一文件，且仅用于保留摘要模式")
    manifest, sections = [], []
    from akb.summarizing import summarize_evidence

    for source_id in ids:
        source = services.get_source(session, source_id)
        if source.object_key.startswith("native:"):
            raise HTTPException(409, "原生文件请在文件管理页面回收；当前不支持跨系统授权彻底删除")
        if source.status in {"queued", "running"}:
            raise HTTPException(409, "资料仍在处理，请稍后创建清理方案")
        item = {
            k: getattr(source, k)
            for k in ("id", "title", "filename", "sha256", "revision", "size", "coverage", "generation_id")
        }
        entry = session.scalar(select(CatalogEntry).where(CatalogEntry.source_id == source.id))
        item["catalog_manual"] = dict(entry.manual) if entry else {}
        if body.mode == "summarize":
            summary = summarize_evidence(session, source)
            if not summary["excerpts"]:
                raise HTTPException(422, "文件没有可用原文，无法生成保留摘要；可选择仅删除")
            item["excerpts"] = summary["excerpts"]
            if source.id in agent_summaries:
                agent_summary = agent_summaries[source.id]
                if not agent_summary.content.strip():
                    raise HTTPException(422, "归纳内容不能为空")
                citations = []
                for citation in agent_summary.citations:
                    evidence = services.get_evidence(session, citation.evidence_id)
                    if (
                        evidence.source_id != source.id
                        or not citation.quote.strip()
                        or citation.quote not in evidence.text
                    ):
                        raise HTTPException(422, "归纳总结的引用必须来自对应文件的当前原文")
                    citations.append(
                        {"evidence_id": evidence.id, "quote": citation.quote, "locator": evidence.locator}
                    )
                item["agent_summary"] = {
                    "content": agent_summary.content,
                    "citations": citations,
                    "kind": "agent_generated_unverified",
                }
                quote_text = "\n".join(c["quote"] for c in citations)
                sections.append(
                    f"## {source.title}\n\nAgent 归纳（需用户审阅）：\n{agent_summary.content}\n\n引用原文片段：\n{quote_text}"
                )
            else:
                sections.append(f"## {source.title}\n\n原文抽样（有损）：\n{summary['summary']}")
            if item["catalog_manual"]:
                import json

                sections.append(
                    "用户目录修改（导航信息，非事实证据）：\n"
                    + json.dumps(item["catalog_manual"], ensure_ascii=False)
                )
        manifest.append(item)
    plan = CleanupPlan(
        tenant_id=tenant_id,
        mode=body.mode,
        manifest=manifest,
        summary="\n\n".join(sections),
        expires_at=now() + timedelta(hours=24),
    )
    session.add(plan)
    session.flush()
    services.audit(session, tenant_id, "storage.plan.create", plan.id)
    return plan


def get_plan(session, plan_id):
    plan = session.get(CleanupPlan, plan_id)
    if not plan:
        raise HTTPException(404, "清理方案不存在")
    return plan


def plan_dict(plan):
    return {
        "id": plan.id,
        "mode": plan.mode,
        "status": plan.status,
        "files": plan.manifest,
        "summary": plan.summary,
        "estimated_release_bytes": sum(s["size"] for s in plan.manifest),
        "expires_at": plan.expires_at,
        "result": plan.result,
        "loss_notice": "摘要有损，不能恢复原件；删除后原有证据、个人事实支持和依赖页面失效。摘要仅作为归档记录。",
    }


def validate_plan(session, plan):
    if plan.expires_at < now():
        raise HTTPException(409, "清理方案已过期，请重新生成")
    for item in plan.manifest:
        source = services.get_source(session, item["id"])
        entry = session.scalar(select(CatalogEntry).where(CatalogEntry.source_id == source.id))
        manual = dict(entry.manual) if entry else {}
        if (
            source.revision != item["revision"]
            or source.sha256 != item["sha256"]
            or source.generation_id != item["generation_id"]
            or manual != item["catalog_manual"]
        ):
            raise HTTPException(409, "文件已变化，请重新生成并确认清理方案")


def approve_plan(session, tenant_id, plan_id, user_id, acknowledge):
    services.lock_tenant(session, tenant_id)
    plan = get_plan(session, plan_id)
    if plan.status != "pending" or not acknowledge:
        raise HTTPException(409, "方案不可授权，或尚未确认删除及信息损失")
    validate_plan(session, plan)
    plan.status, plan.authorizer_id = "authorized", user_id
    plan.expires_at = now() + timedelta(hours=1)
    services.audit(session, tenant_id, "storage.plan.authorize", plan.id)
    return plan


def execute_plan(session, tenant_id, plan_id):
    services.lock_tenant(session, tenant_id)
    plan = get_plan(session, plan_id)
    if plan.status == "completed":
        return plan
    if plan.status != "authorized" or not plan.authorizer_id:
        raise HTTPException(403, "需要用户在网页中批准这份具体方案，Agent 不能自行授权")
    validate_plan(session, plan)
    archive_id = None
    if plan.mode == "summarize":
        archive = StorageArchive(
            tenant_id=tenant_id,
            title=f"资料摘要归档 · {len(plan.manifest)} 份",
            content=plan.summary,
            manifest=plan.manifest,
            plan_id=plan.id,
        )
        session.add(archive)
        session.flush()
        archive_id = archive.id
        session.scalar(select(Catalog)).revision += 1
    for item in plan.manifest:
        services.revoke_source(session, tenant_id, item["id"])
    plan.status = "completed"
    plan.result = {
        "archive_id": archive_id,
        "cleanup": "queued",
        "bytes_released_now": 0,
        "message": "原件访问已撤回，实际空间将在后台删除原件后释放",
    }
    services.audit(session, tenant_id, "storage.plan.execute", plan.id)
    return plan
