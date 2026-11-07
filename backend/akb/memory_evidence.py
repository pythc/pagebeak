"""Versioned character anchors into originals, never into generated summaries."""

from uuid import UUID

from fastapi import HTTPException
from pydantic import Field

from akb import services
from akb.evidence_text import source_span
from akb.models import Evidence
from akb.schemas import Input


class EvidenceAnchor(Input):
    evidence_id: UUID
    source_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    quote: str = Field(min_length=1, max_length=12000)
    start: int | None = Field(default=None, ge=0)


class FactCitation(Input):
    statement: str = Field(min_length=1, max_length=4000)
    evidence: EvidenceAnchor


def resolve(session, anchor):
    from akb.memory_library import source_version

    ev = session.scalar(services.evidence_query().where(Evidence.id == str(anchor.evidence_id)))
    if not ev:
        raise HTTPException(404, "原文片段不存在、已失效或不属于当前用户")
    src = services.get_source(session, ev.source_id)
    from akb.local_catalog import ensure_current

    ensure_current(session, src.id)
    if source_version(src) != anchor.source_version:
        raise HTTPException(409, "原资料版本已变化，请重新读取原文")
    raw, mode = source_span(ev.text, anchor.quote)
    if raw is None:
        raise HTTPException(422, "引文不能唯一定位到原文；请重新读取，不要删去限定条件")
    start = ev.text.find(raw) if anchor.start is None else anchor.start
    if ev.text[start : start + len(raw)] != raw:
        raise HTTPException(422, "引文起点与原文不一致")
    if anchor.start is None and ev.text.find(raw, start + 1) >= 0:
        raise HTTPException(422, "引文重复出现，请提供字符 start 或更完整的片段")
    return {
        "evidence_id": ev.id,
        "source_id": src.id,
        "source_title": src.title,
        "source_version": anchor.source_version,
        "locator": ev.locator,
        "start": start,
        "end": start + len(raw),
        "quote": raw,
        "match": mode,
        "scope": src.subject_scope,
        "coverage": src.coverage,
        "verification": "original_span_only; semantic_entailment_not_certified",
    }


def anchors_for_page(session, body):
    resolved = []
    for citation in body.citations:
        if citation.statement not in body.content:
            raise HTTPException(422, "事实出处所对应的 statement 必须存在于知识页正文")
        item = resolve(session, citation.evidence)
        if body.source_versions.get(item["source_id"]) != item["source_version"]:
            raise HTTPException(422, "事实出处必须属于该页绑定的来源版本")
        resolved.append({"statement": citation.statement, **item})
    return resolved


def read_citations(session, entries):
    # Metadata contains only IDs, source versions and offsets. Original text remains
    # in the evidence store and generated statements remain in Tencent.
    result = []
    for item in entries:
        ev = session.scalar(services.evidence_query().where(Evidence.id == item["evidence_id"]))
        if not ev:
            result.append({**item, "current": False, "quote": None})
            continue
        from akb.memory_library import source_version

        src = services.get_source(session, ev.source_id)
        valid = source_version(src) == item["source_version"]
        result.append(
            {
                **item,
                "current": valid,
                "source_title": src.title,
                "locator": ev.locator,
                "quote": ev.text[item["start"] : item["end"]] if valid else None,
            }
        )
    return result
