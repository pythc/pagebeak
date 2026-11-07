"""Bounded extractive summaries spanning all parsed blocks; no model-generated claims."""

import re
from collections import Counter

from sqlalchemy import select

from akb.models import Evidence


def summarize_evidence(session, source):
    rows = (
        list(
            session.scalars(
                select(Evidence)
                .where(Evidence.source_id == source.id, Evidence.generation_id == source.generation_id)
                .order_by(Evidence.ordinal)
            )
        )
        if source.status in {"ready", "partial"}
        else []
    )
    fragments = []
    seen = set()
    for evidence in rows:
        for sentence in re.split(r"(?<=[。！？.!?])\s*|\n+", evidence.text):
            text = sentence.strip()
            if text and text not in seen:
                seen.add(text)
                fragments.append((evidence, text[:240]))
    # Keep an ordered sample across the whole parsed document rather than only its first lines.
    indexes = (
        sorted(
            {
                round(i * (len(fragments) - 1) / min(7, len(fragments) - 1))
                for i in range(min(8, len(fragments)))
            }
        )
        if len(fragments) > 1
        else [0]
        if fragments
        else []
    )
    excerpts = [
        {"evidence_id": fragments[i][0].id, "quote": fragments[i][1], "locator": fragments[i][0].locator}
        for i in indexes
    ]
    tokens = Counter()
    for evidence in rows:
        tokens.update(re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,30}|[\u4e00-\u9fff]{2,6}", evidence.text))
    return {
        "summary": "\n".join(e["quote"] for e in excerpts),
        "excerpts": excerpts,
        "summary_kind": "document_extracts",
        "tags": [t for t, _ in tokens.most_common(8)],
        "blocks_scanned": len(rows),
        "coverage": source.coverage,
    }
