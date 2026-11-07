"""Copy existing human edits into protected Tencent memories; enqueue original exports.

Run with API/worker stopped and a verified PostgreSQL backup. No original or legacy
row is removed. Model-generated legacy claims are intentionally not re-certified.
"""
import argparse
import json
from uuid import NAMESPACE_URL, uuid5

from akb import memory_library as memory
from akb.config import Settings
from akb.db import Database
from akb.models import (
    Catalog,
    CatalogEntry,
    Claim,
    Decision,
    KnowledgeDecision,
    KnowledgeFact,
    MemoryObject,
    User,
)
from akb.organization import enqueue
from akb.security import SCOPES, Principal
from sqlalchemy import create_engine, select


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = Settings().model_copy(update={"knowledge_engine": "tencent"})
    engine = create_engine(settings.migration_database_url)
    with engine.connect() as connection:
        users = list(connection.execute(select(User.id, User.tenant_id)))
    db = Database(settings.database_url)
    report = {"tenants": len(users), "manual_records": 0, "copied": 0, "mode": "apply" if args.apply else "preview"}
    for user, tenant in users:
        with db.session(tenant, actor="migration") as session:
            sources = memory.live_sources(session)
            records = []
            for model in (Catalog, CatalogEntry):
                for row in session.scalars(select(model)):
                    if row.manual and (not hasattr(row, "source_id") or row.source_id in sources):
                        records.append((row.id, "人工目录记录", row.manual, getattr(row, "source_id", None)))
            for row in session.scalars(select(KnowledgeFact).where(KnowledgeFact.basis == "user_asserted")):
                if row.source_id and row.source_id not in sources:
                    continue
                records.append((row.id, "人工事实记录", {"field": row.field, "value": row.value,
                    "status": row.status, "quote": row.quote}, row.source_id))
            for row in session.scalars(select(Decision)):
                claim = session.get(Claim, row.claim_id)
                if claim and claim.source_id in sources:
                    records.append((row.id, "人工复核记录", {"action": row.action, "field": claim.field,
                        "value": row.new_value, "previous_value": row.previous_value}, claim.source_id))
            # Include accept/reject history, not only corrected user_asserted facts.
            # Older rows predate actor attribution: preserve them without inventing an actor.
            for row in session.scalars(select(KnowledgeDecision)):
                if row.detail.get("actor") == "agent":
                    continue
                fact = session.get(KnowledgeFact, row.fact_id)
                if fact and (not fact.source_id or fact.source_id in sources):
                    records.append((row.id, "既有复核记录", {"action": row.action, "field": fact.field,
                        "value_at_migration": fact.value, "status_at_migration": fact.status,
                        "detail": row.detail, "actor": row.detail.get("actor", "historical_unspecified")}, fact.source_id))
            report["manual_records"] += len(records)
            if not args.apply:
                continue
            memory.services.lock_tenant(session, tenant)
            principal = Principal(user, tenant, "", "migration", set(SCOPES), "browser", "")
            for record_id, title, detail, sid in records:
                key = "legacy-manual-" + record_id
                if session.scalar(select(MemoryObject.id).where(MemoryObject.key == key)):
                    continue
                content = "旧库人工修改的迁移快照。保留原有决定；这是历史上下文，用于填写时仍需回到当前原文核对。\n\n"
                content += json.dumps(detail, ensure_ascii=False, indent=2)
                memory.write(session, settings, principal, memory.MemoryWrite(
                    request_id=uuid5(NAMESPACE_URL, tenant + "/" + key), kind="episode", key=key,
                    title=title, content=content, expected_revision=0,
                    source_versions={sid: memory.source_version(sources[sid])} if sid else {}))
                report["copied"] += 1
            enqueue(session, tenant, force=True)
    db.close()
    engine.dispose()
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
