"""Provision runtime roles and the isolated test database; migrate separately."""

import psycopg
from akb.config import Settings
from psycopg import sql
from sqlalchemy.engine import make_url

settings = Settings()
admin_url = make_url(settings.migration_database_url).set(drivername="postgresql", database="postgres")
with psycopg.connect(admin_url.render_as_string(hide_password=False), autocommit=True) as connection:
    for role, url in [("akb_app", settings.database_url), ("akb_worker", settings.worker_database_url)]:
        password = make_url(url).password
        exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        connection.execute(
            sql.SQL(
                verb + " ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}"
            ).format(sql.Identifier(role), sql.Literal(password))
        )
    if not connection.execute("SELECT 1 FROM pg_database WHERE datname='akb_test'").fetchone():
        connection.execute("CREATE DATABASE akb_test")
print("Runtime roles and isolated test database ready; no application tables changed")
