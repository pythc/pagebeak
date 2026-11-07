"""Upgrade and failed downgrade on an isolated database, never a running user database."""
import os
import subprocess
from datetime import timedelta
from uuid import uuid4

import psycopg
from akb.config import ROOT
from akb.models import Catalog, SpaceDevice, Tenant, User, now
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


def test_upgrade_preserves_existing_space_and_destructive_downgrade_is_refused(test_settings):
    name = 'akb_identity_migration_' + uuid4().hex[:10]
    admin_url = make_url(test_settings.migration_database_url).set(drivername='postgresql', database='postgres')
    with psycopg.connect(admin_url.render_as_string(hide_password=False), autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            url = make_url(test_settings.migration_database_url).set(database=name).render_as_string(hide_password=False)
            env = {**os.environ, 'AKB_MIGRATION_DATABASE_URL': url}

            def migrate(target, direction='upgrade'):
                return subprocess.run([str(ROOT / '.venv/bin/alembic'), direction, target],
                                      cwd=ROOT, env=env, capture_output=True)

            assert migrate('0026').returncode == 0, 'Baseline schema migration failed'
            tenant, user_id, device_id = (str(uuid4()) for _ in range(3))
            engine = create_engine(url)
            try:
                with Session(engine) as session, session.begin():
                    session.add(User(id=user_id, email=uuid4().hex + '@example.test', display_name='Existing user',
                                     password_hash='unchanged-synthetic-password-hash', tenant_id=tenant))
                    session.add(Tenant(id=tenant))
                    session.flush()
                    session.add(Catalog(tenant_id=tenant, manual={'notes': 'existing manual changes'}))
                    session.add(SpaceDevice(id=device_id, digest=uuid4().hex, csrf=uuid4().hex,
                                            user_id=user_id, expires_at=now() + timedelta(days=1)))
                assert migrate('head').returncode == 0, 'Identity schema upgrade failed'
                assert migrate('0026', 'downgrade').returncode != 0, 'Destructive downgrade must be refused'
                with Session(engine) as session:
                    assert session.get(User, user_id).tenant_id == tenant
                    assert session.get(User, user_id).password_hash == 'unchanged-synthetic-password-hash'
                    assert session.get(SpaceDevice, device_id).user_id == user_id
                    from sqlalchemy import select, text
                    assert session.scalar(select(Catalog).where(Catalog.tenant_id == tenant)).manual == {
                        'notes': 'existing manual changes'}
                    assert session.execute(text('SELECT version_num FROM alembic_version')).scalar() == '0027'
            finally:
                engine.dispose()
        finally:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
