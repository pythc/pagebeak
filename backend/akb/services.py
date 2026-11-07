import hashlib
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import case, delete, or_, select

from akb.models import (
    Audit,
    CatalogEntry,
    Claim,
    CleanupPlan,
    Decision,
    Evidence,
    FormDraft,
    Idempotency,
    Job,
    Page,
    Source,
    Tenant,
    now,
    uid,
)
from akb.parsing import FIELDS


def lock_tenant(session, tenant_id):
    tenant = session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if not tenant:
        raise HTTPException(404, "资料库不存在")
    return tenant


def audit(session, tenant_id, action, resource_id):
    session.add(Audit(tenant_id=tenant_id, action=action, resource_id=resource_id))


def get_source(session, source_id):
    source = session.get(Source, source_id)
    if not source or source.revoked_at:
        raise HTTPException(404, "资料不存在或已撤回")
    return source


def source_dict(source):
    return {
        k: getattr(source, k)
        for k in (
            "id",
            "title",
            "filename",
            "media_type",
            "size",
            "sha256",
            "subject_scope",
            "status",
            "coverage",
            "error",
            "category",
            "revision",
            "created_at",
            "folder_id",
            "trashed_at",
            "origin_path",
        )
    }


def evidence_query():
    return (
        select(Evidence)
        .join(Source, (Evidence.source_id == Source.id) & (Evidence.tenant_id == Source.tenant_id))
        .where(
            Source.revoked_at.is_(None),
            Source.generation_id == Evidence.generation_id,
            Source.status.in_(["ready", "partial"]),
        )
    )


def get_evidence(session, evidence_id):
    evidence = session.scalar(evidence_query().where(Evidence.id == evidence_id))
    if not evidence:
        raise HTTPException(404, "证据不存在或已撤回")
    return evidence


def evidence_dict(session, evidence):
    source = get_source(session, evidence.source_id)
    return {
        "id": evidence.id,
        "source_id": source.id,
        "source_title": source.title,
        "locator": evidence.locator,
        "text": evidence.text,
        "generation_id": evidence.generation_id,
    }


def object_path(settings, object_key):
    root = settings.object_root()
    target = (root / object_key).resolve()
    if not object_key or not target.is_relative_to(root) or target == root:
        raise HTTPException(404, "原件不存在")
    return target


def register_source(
    session,
    settings,
    tenant_id,
    filename,
    media_type,
    data,
    subject_scope,
    key=None,
    *,
    staged=None,
    source_id=None,
):
    tenant = lock_tenant(session, tenant_id)
    if staged is not None:
        hasher = hashlib.sha256()
        with staged.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        sha, size = hasher.hexdigest(), staged.stat().st_size
    else:
        sha, size = hashlib.sha256(data).hexdigest(), len(data)
    fingerprint = hashlib.sha256(
        json.dumps([filename, media_type, sha, subject_scope], ensure_ascii=False).encode()
    ).hexdigest()
    if key:
        entry = session.scalar(
            select(Idempotency).where(Idempotency.operation == "upload", Idempotency.key == key)
        )
        if entry:
            if entry.request_hash != fingerprint:
                raise HTTPException(409, "相同幂等键对应了不同的上传内容")
            source = get_source(session, entry.resource_id)
            job = session.scalar(
                select(Job)
                .where(Job.source_id == source.id, Job.kind == "ingest")
                .order_by(Job.created_at.desc())
            )
            return source, job, True
    if size > settings.upload_limit:
        raise HTTPException(413, "单文件超过 2 GiB 上限")
    if tenant.used_bytes + tenant.reserved_bytes + tenant.memory_bytes + size > tenant.quota_bytes:
        raise HTTPException(413, "个人空间不足，请释放空间或联系管理员扩容")
    source_id = source_id or uid()
    object_key = f"{tenant_id}/{source_id}/original"
    path = object_path(settings, object_key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if staged is not None:
        os.link(staged, path)
        session.info.setdefault("created_objects", []).append(path)
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        session.info.setdefault("created_objects", []).append(path)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    if settings.cloudreve_credentials:
        from akb.object_store import publish

        object_key = publish(settings, tenant_id, source_id, path, session)
        # The original now lives in Cloudreve. The staging file remains until transaction commit.
        path.unlink()
    source = Source(
        id=source_id,
        tenant_id=tenant_id,
        filename=Path(filename).name[:255],
        title=Path(filename).name[:255],
        media_type=media_type[:120],
        object_key=object_key,
        size=size,
        sha256=sha,
        subject_scope=subject_scope,
    )
    session.add(source)
    session.flush()
    job = Job(tenant_id=tenant_id, source_id=source.id, source_revision=1)
    session.add(job)
    session.flush()
    if key:
        session.add(
            Idempotency(
                tenant_id=tenant_id,
                operation="upload",
                key=key,
                request_hash=fingerprint,
                resource_id=source.id,
            )
        )
    tenant.revision += 1
    tenant.used_bytes += size
    from akb import changes

    changes.record(
        session,
        tenant_id,
        source.id,
        "create",
        session.info.get("actor", "system"),
        {},
        changes.snapshot(source),
        source.id,
    )
    audit(session, tenant_id, "source.upload", source.id)
    from akb.organization import enqueue

    enqueue(session, tenant_id)
    return source, job, False


def revoke_source(session, tenant_id, source_id):
    tenant = lock_tenant(session, tenant_id)
    source = session.get(Source, source_id)
    if not source:
        raise HTTPException(404, "资料不存在或已撤回")
    if source.revoked_at:
        return
    if source.object_key.startswith("native:"):
        raise HTTPException(409, "请在文件管理页面移入回收站，知识库会自动同步撤回")
    from akb import changes

    before = changes.snapshot(source)
    source.revoked_at, source.status = now(), "revoked"
    source.revision += 1
    changes.record(
        session,
        tenant_id,
        source.id,
        "delete",
        session.info.get("actor", "system"),
        before,
        changes.snapshot(source),
        source.id,
    )
    tenant.revision += 1
    for job in session.scalars(
        select(Job).where(Job.source_id == source.id, Job.status.in_(["queued", "running"])).with_for_update()
    ):
        job.status, job.fencing_token = "cancelled", job.fencing_token + 1
    session.add(
        Job(tenant_id=tenant_id, source_id=source.id, source_revision=source.revision, kind="cleanup")
    )
    audit(session, tenant_id, "source.revoke", source.id)
    from akb.organization import enqueue

    enqueue(session, tenant_id)


def available_claims(session):
    claims = list(session.scalars(select(Claim).order_by(Claim.created_at)))
    live_evidence = {e.id for e in session.scalars(evidence_query())}
    return [c for c in claims if c.source_id is None or c.evidence_id in live_evidence]


def claim_dict(claim):
    return {
        k: getattr(claim, k)
        for k in ("id", "field", "value", "quote", "status", "basis", "revision", "source_id", "evidence_id")
    }


def profile(session, tenant_id):
    tenant = session.get(Tenant, tenant_id)
    all_claims = available_claims(session)
    grouped = defaultdict(list)
    for claim in all_claims:
        if claim.status == "accepted":
            grouped[claim.field].append(claim)
    fields = {}
    for field, label in FIELDS.items():
        claims = grouped[field]
        manual = [c for c in claims if c.source_id is None and c.basis == "user_asserted"]
        if manual:
            claims = manual[-1:]
        values = {c.value.casefold().strip() for c in claims}
        fields[field] = {
            "label": label,
            "status": "ready" if len(values) == 1 else "conflict" if values else "missing",
            "value": claims[0].value if len(values) == 1 else None,
            "claims": [claim_dict(c) for c in claims],
        }
    from akb import knowledge
    from akb.knowledge_schema import FIELDS as KNOWLEDGE_FIELDS
    from akb.knowledge_schema import KnowledgeSelector

    semantic = knowledge.live_facts(session)
    for field, kind in knowledge.PROFILE_KEYS.items():
        current = fields.get(field)
        if current and any(c["basis"] == "user_asserted" for c in current["claims"]):
            continue
        related = [f for f in semantic if f.kind == kind and f.field == field
                   and f.subject_scope == "self" and f.status == "accepted"]
        if related or field not in fields:
            resolved = knowledge.answer(session, KnowledgeSelector(kind=kind, field=field))
            fields[field] = {"label": KNOWLEDGE_FIELDS[kind][field], **resolved}
    sentences = [f"{v['label']}：{v['value']}" for v in fields.values() if v["status"] == "ready"]
    return {
        "revision": tenant.revision,
        "fields": fields,
        "introduction": "；".join(sentences),
        "candidates": [claim_dict(c) for c in all_claims if c.status == "candidate"],
        "mode": "structured_facts",
        "scope": "当前个人资料库中可用且已采纳的事实",
        "knowledge_candidate_count": sum(f.status == "candidate" for f in semantic),
    }


def decide_claim(session, tenant_id, claim_id, action, expected_revision, value=None):
    tenant = lock_tenant(session, tenant_id)
    claim = session.get(Claim, claim_id)
    live = {c.id for c in available_claims(session)}
    if not claim or claim.id not in live:
        raise HTTPException(404, "事实候选不存在或其来源已撤回")
    if claim.revision != expected_revision:
        raise HTTPException(409, "事实已变化，请刷新后重试")
    previous = claim.value
    if action == "correct":
        if not value or not value.strip():
            raise HTTPException(422, "修正值不能为空")
        for old in session.scalars(
            select(Claim).where(
                Claim.field == claim.field, Claim.source_id.is_(None), Claim.status == "accepted"
            )
        ):
            old.status, old.revision = "superseded", old.revision + 1
        session.add(
            Claim(
                tenant_id=tenant_id,
                field=claim.field,
                value=value.strip(),
                status="accepted",
                basis="user_asserted",
            )
        )
        claim.status = "rejected"
    else:
        claim.status = "accepted" if action == "accept" else "rejected"
        if action == "accept":
            claim.basis = "user_confirmed"
    claim.revision += 1
    session.add(
        Decision(
            tenant_id=tenant_id, claim_id=claim.id, action=action, previous_value=previous, new_value=value
        )
    )
    tenant.revision += 1
    audit(session, tenant_id, f"claim.{action}", claim.id)


def propose_claims(session, tenant_id, source_id, expected_revision, items):
    tenant = lock_tenant(session, tenant_id)
    source = get_source(session, source_id)
    if source.subject_scope != "self":
        raise HTTPException(422, "这份资料未声明描述本人，不能用于个人事实抽取")
    if source.revision != expected_revision:
        raise HTTPException(409, "资料版本已变化")
    created = []
    for item in items:
        evidence = get_evidence(session, item.evidence_id)
        if evidence.source_id != source_id or item.quote not in evidence.text or item.value not in item.quote:
            raise HTTPException(422, "候选值与原文证据不匹配")
        existing = session.scalar(
            select(Claim).where(
                Claim.field == item.field, Claim.value == item.value, Claim.evidence_id == evidence.id
            )
        )
        if existing:
            created.append(existing.id)
            continue
        claim = Claim(
            tenant_id=tenant_id,
            source_id=source_id,
            evidence_id=evidence.id,
            field=item.field,
            value=item.value,
            quote=item.quote,
            basis="agent_proposed",
            status="candidate",
        )
        session.add(claim)
        session.flush()
        created.append(claim.id)
    tenant.revision += 1
    audit(session, tenant_id, "claims.propose", source_id)
    return created


def search(session, query, limit=10):
    words = re.findall(r"[a-zA-Z0-9@._-]+|[\u4e00-\u9fff]+", query.casefold())
    terms = []
    for word in words:
        terms.append(word)
        if re.search(r"[\u4e00-\u9fff]", word):
            terms.extend(word[i : i + 2] for i in range(len(word) - 1))
    terms = list(dict.fromkeys(terms))[:30]
    if not terms:
        return []
    expressions = [
        Evidence.text.ilike(
            "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", escape="\\"
        )
        for t in terms
    ]
    score = sum(case((expr, 1), else_=0) for expr in expressions)
    rows = session.scalars(
        evidence_query().where(or_(*expressions)).order_by(score.desc(), Evidence.id).limit(limit)
    )
    return [evidence_dict(session, e) for e in rows]


def save_page(session, tenant_id, title, content, evidence_ids, fact_ids=None, expected_revision=None):
    tenant = lock_tenant(session, tenant_id)
    if expected_revision is not None and expected_revision != tenant.revision:
        raise HTTPException(409, "知识已变化，请核对后重新生成页面")
    from akb.knowledge import live_facts

    valid = {f.id for f in live_facts(session) if f.status == "accepted"}
    if not set(fact_ids or []).issubset(valid):
        raise HTTPException(422, "专题只能关联当前已采纳事实")
    for evidence_id in evidence_ids:
        get_evidence(session, evidence_id)
    page = Page(
        tenant_id=tenant_id, title=title, content=content, evidence_ids=list(dict.fromkeys(evidence_ids)),
        snapshot_revision=tenant.revision, fact_ids=list(dict.fromkeys(fact_ids or [])),
    )
    session.add(page)
    session.flush()
    # Generated pages do not change source facts used by existing form drafts.
    audit(session, tenant_id, "page.create", page.id)
    return page


def visible_pages(session):
    valid = {e.id for e in session.scalars(evidence_query())}
    revision = session.scalar(select(Tenant.revision))
    return [
        {
            "id": p.id,
            "title": p.title,
            "content": p.content if p.snapshot_revision == revision else "",
            "stale": p.snapshot_revision != revision,
            "message": "知识已变化或该历史页面未绑定版本，请重新核对并生成" if p.snapshot_revision != revision else "Agent 生成内容，引用存在不代表全部推断已验证",
            "snapshot_revision": p.snapshot_revision,
            "fact_ids": p.fact_ids,
            "evidence_ids": p.evidence_ids,
            "origin_kind": "generated",
            "created_at": p.created_at,
        }
        for p in session.scalars(select(Page).order_by(Page.created_at.desc()))
        if p.evidence_ids and set(p.evidence_ids).issubset(valid)
    ]


def prepare_form(session, tenant_id, title, fields):
    from akb import form_answers
    from akb.knowledge import answer

    tenant = lock_tenant(session, tenant_id)
    current = profile(session, tenant_id)
    by_key = {}
    for field in fields:
        if field.narrative:
            continue
        fact = answer(session, field.knowledge) if field.knowledge else current["fields"].get(field.fact_key)
        result = {
            "key": field.key, "label": field.label, "fact_key": field.fact_key,
            "status": fact["status"] if fact else "missing", "value": fact["value"] if fact else None,
            "reason": fact.get("reason", "来自当前已采纳事实") if fact else "资料库中没有可用的已采纳事实",
            "evidence_ids": list(dict.fromkeys(c["evidence_id"] for c in fact["claims"] if c["evidence_id"])) if fact else [],
            "claims": fact["claims"] if fact else [],
            "calculation": fact.get("calculation") if fact else None,
            "pending_tasks": fact.get("pending_tasks", 0) if fact else 0,
        }
        form_answers.apply_contract(field, result)
        effective_key = field.knowledge.field if field.knowledge else field.fact_key
        if effective_key in {"phone", "email"} and not field.allow_sensitive:
            if result["status"] == "ready":
                form_answers.block(result, "此字段需要显式允许使用联系方式")
        form_answers.apply_constraints(field, result)
        by_key[field.key] = result
    for field in fields:
        if field.narrative:
            by_key[field.key] = form_answers.render_narrative(field, by_key)
    answers = [dict(by_key[field.key], contract_version="form-evidence-v2") for field in fields]
    draft = FormDraft(
        tenant_id=tenant_id,
        title=title,
        fields=[f.model_dump(mode="json") for f in fields],
        answers=answers,
        snapshot_revision=tenant.revision,
    )
    session.add(draft)
    session.flush()
    audit(session, tenant_id, "form.prepare", draft.id)
    return draft


def draft_dict(session, tenant_id, draft):
    current = session.get(Tenant, tenant_id).revision
    stale = current != draft.snapshot_revision or any(a.get("contract_version") != "form-evidence-v2" for a in draft.answers)
    return {
        "id": draft.id,
        "title": draft.title,
        "snapshot_revision": draft.snapshot_revision,
        "stale": stale,
        "answers": [] if stale else draft.answers,
        "message": "资料或填写规则已变化，请重新生成草稿" if stale else "仅准备填写草稿，尚未对外提交",
        "fields": draft.fields,
        "contract_version": "form-evidence-v2",
    }


def clean_source(session, settings, source):
    # Privacy cleanup is unconditional, even when automatic organization is paused.
    from akb.knowledge import purge_source

    purge_source(session, source.id)
    session.execute(delete(CatalogEntry).where(CatalogEntry.source_id == source.id))
    for plan in session.scalars(select(CleanupPlan).where(CleanupPlan.status.in_(["pending", "authorized"]))):
        if any(item["id"] == source.id for item in plan.manifest):
            plan.status, plan.summary, plan.manifest = "cancelled", "", []
    evidence_ids = set(session.scalars(select(Evidence.id).where(Evidence.source_id == source.id)))
    claim_ids = list(session.scalars(select(Claim.id).where(Claim.source_id == source.id)))
    if claim_ids:
        session.execute(delete(Decision).where(Decision.claim_id.in_(claim_ids)))
    session.execute(delete(Claim).where(Claim.source_id == source.id))
    session.execute(delete(Evidence).where(Evidence.source_id == source.id))
    for page in session.scalars(select(Page)):
        if evidence_ids.intersection(page.evidence_ids):
            session.delete(page)
    for draft in session.scalars(select(FormDraft)):
        draft.answers = []
    if source.object_key:
        from akb.object_store import delete_original

        delete_original(settings, source.object_key)
        tenant = lock_tenant(session, source.tenant_id)
        tenant.used_bytes -= source.size
    source.object_key, source.sha256, source.filename, source.title = "", "", "已删除", "已删除"
    source.status, source.error, source.generation_id = "deleted", None, None
