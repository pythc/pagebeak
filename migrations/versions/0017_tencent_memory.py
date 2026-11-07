"""Tencent is the body store; PostgreSQL retains tenancy and publication fences."""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE memory_bindings (
      id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES tenants(id),
      wiki_id VARCHAR(100) NOT NULL, exports JSON NOT NULL, UNIQUE(tenant_id));
    CREATE TABLE memory_objects (
      id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES tenants(id),
      kind VARCHAR(20) NOT NULL, key VARCHAR(100) NOT NULL, title VARCHAR(200) NOT NULL,
      revision INTEGER NOT NULL, remote_ref VARCHAR(250) NOT NULL, remote_ids JSON NOT NULL,
      source_versions JSON NOT NULL, content_hash VARCHAR(64) NOT NULL,
      protected BOOLEAN NOT NULL, deleted BOOLEAN NOT NULL, expires_at TIMESTAMPTZ,
      updated_at TIMESTAMPTZ NOT NULL, UNIQUE(tenant_id,kind,key));
    CREATE TABLE memory_garbage (
      id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES tenants(id),
      kind VARCHAR(20) NOT NULL, remote_ref VARCHAR(250) NOT NULL,
      remote_ids JSON NOT NULL, created_at TIMESTAMPTZ NOT NULL);
    """)
    for table in ("memory_bindings", "memory_objects", "memory_garbage"):
        op.execute(f"CREATE INDEX ix_{table}_tenant_id ON {table}(tenant_id)")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {table} USING (tenant_id = current_setting('akb.tenant_id',true)) WITH CHECK (tenant_id = current_setting('akb.tenant_id',true))")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO akb_app, akb_worker")


def downgrade():
    raise RuntimeError("Keep memory data on application rollback; destructive downgrade is disabled.")
