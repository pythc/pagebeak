"""Dry run by default. Collect only journaled failed uploads after a 24-hour grace period."""

import argparse
import json
from datetime import UTC, datetime, timedelta

from akb.config import Settings
from akb.db import Database
from akb.models import Source
from akb.object_store import Cloudreve, digest
from akb.services import lock_tenant


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    db = Database(settings.worker_database_url)
    provider = Cloudreve(settings)
    counts = {"referenced": 0, "grace_period": 0, "orphaned": 0, "collected": 0}
    try:
        for journal in (settings.object_root().parent / "cloudreve-journal").glob("*.json"):
            record = json.loads(journal.read_text())
            with db.session(record["tenant_id"]) as session:
                lock_tenant(session, record["tenant_id"])
                source = session.get(Source, record["source_id"])
                if source and source.object_key == record["key"]:
                    counts["referenced"] += 1
                    if args.apply:
                        journal.unlink()
                elif datetime.fromisoformat(record["created_at"]) > datetime.now(UTC) - timedelta(hours=24):
                    counts["grace_period"] += 1
                else:
                    counts["orphaned"] += 1
                    if args.apply:
                        response = provider.request("HEAD", provider.path(record["key"]))
                        if response.status_code != 404:
                            with provider.materialize(record["key"]) as path:
                                if digest(path) != record["sha256"]:
                                    raise RuntimeError("Object changed; refuse orphan cleanup")
                            provider.delete(record["key"])
                        journal.unlink()
                        counts["collected"] += 1
        print(json.dumps({"mode": "apply" if args.apply else "dry_run", **counts}))
    finally:
        provider.close()
        db.close()


if __name__ == "__main__":
    main()
