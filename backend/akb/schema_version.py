"""Read-only deployment fence. Never migrate or downgrade data implicitly."""

from sqlalchemy import create_engine, text

from akb.config import ROOT, Settings


def check_schema():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    expected = set(ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_heads())
    # Normal startup only reads the migration marker; never needs the schema owner.
    engine = create_engine(Settings().database_url, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as connection:
            actual = set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
        if actual != expected:
            raise RuntimeError(
                "数据库版本与本版程序不符；先备份并执行 .venv/bin/alembic upgrade head，再启动"
            )
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("数据库版本未能核验；请检查数据库和迁移，应用尚未启动") from None
    finally:
        engine.dispose()
