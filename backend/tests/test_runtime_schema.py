import pytest
from akb import schema_version
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError


def test_runtime_can_check_schema_without_migration_secret(test_settings, monkeypatch):
    settings = test_settings.model_copy(update={"migration_database_url": ""})
    monkeypatch.setattr(schema_version, "Settings", lambda: settings)
    schema_version.check_schema()
    # Reading the marker must not give runtime identities migration privileges.
    for url in (settings.database_url, settings.worker_database_url):
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                from akb.config import ROOT
                from alembic.config import Config
                from alembic.script import ScriptDirectory
                expected = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
                assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == expected
            with pytest.raises(ProgrammingError):
                with engine.begin() as connection:
                    connection.execute(text("UPDATE alembic_version SET version_num=version_num"))
        finally:
            engine.dispose()
