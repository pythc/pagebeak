from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


class Database:
    def __init__(self, url: str):
        if not url.startswith("postgresql+psycopg://"):
            raise ValueError("Configure a PostgreSQL psycopg connection in AKB_DATABASE_URL")
        self.engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)

    @contextmanager
    def session(self, tenant_id: str | None = None, actor: str = "system"):
        with Session(self.engine, expire_on_commit=False) as session:
            session.info["actor"] = actor
            try:
                with session.begin():
                    if tenant_id:
                        session.execute(
                            text("SELECT set_config('akb.tenant_id', :tenant, true)"), {"tenant": tenant_id}
                        )
                    yield session
                for path in session.info.get("published_journals", []):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass  # Reconciliation will see the committed source and remove the journal.
            except BaseException:
                for path in session.info.get("created_objects", []):
                    path.unlink(missing_ok=True)
                raise

    def close(self):
        self.engine.dispose()
