"""Apply frozen migrations on a disposable database and compare to the ORM schema."""

import os
import subprocess
from uuid import uuid4

import psycopg
from akb.config import ROOT, Settings
from akb.models import Base
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

settings = Settings()
name = "akb_migration_" + uuid4().hex[:12]
admin_url = make_url(settings.migration_database_url).set(drivername="postgresql", database="postgres")
with psycopg.connect(admin_url.render_as_string(hide_password=False), autocommit=True) as admin:
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        url = (
            make_url(settings.migration_database_url).set(database=name).render_as_string(hide_password=False)
        )
        completed = subprocess.run(
            [str(ROOT / ".venv/bin/alembic"), "upgrade", "head"],
            cwd=ROOT,
            env={**os.environ, "AKB_MIGRATION_DATABASE_URL": url},
            capture_output=True,
        )
        if completed.returncode:
            detail = completed.stderr.decode(errors="replace")
            for candidate in [
                settings.database_url,
                settings.worker_database_url,
                settings.migration_database_url,
            ]:
                password = make_url(candidate).password
                if password:
                    detail = detail.replace(password, "[redacted]")
            print(detail[-4000:])
            raise RuntimeError("Fresh migration failed; inspect local migration implementation")
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
                assert not differences, "Frozen migration and current models differ"
        finally:
            engine.dispose()
        print("Fresh migrations through head: PASS; ORM schema comparison: PASS")
    finally:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
