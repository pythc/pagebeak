"""Local inventory and original-version metadata; additive and tenant isolated."""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE local_catalogs (
      id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES tenants(id),
      device_id VARCHAR(36) NOT NULL, revision INTEGER NOT NULL, snapshot_id VARCHAR(36) NOT NULL,
      items JSON NOT NULL, updated_at TIMESTAMPTZ NOT NULL, UNIQUE(tenant_id,device_id));
    CREATE TABLE local_source_links (
      id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES tenants(id),
      device_id VARCHAR(36) NOT NULL, document_id VARCHAR(36) NOT NULL, source_id VARCHAR(36) NOT NULL,
      sha256 VARCHAR(64) NOT NULL, current_hash VARCHAR(64), stale BOOLEAN NOT NULL,
      UNIQUE(tenant_id,device_id,document_id,source_id));
    """)
    for table in ("local_catalogs", "local_source_links"):
        op.execute(f"CREATE INDEX ix_{table}_tenant_id ON {table}(tenant_id)")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {table} USING (tenant_id = current_setting('akb.tenant_id',true)) WITH CHECK (tenant_id = current_setting('akb.tenant_id',true))")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO akb_app, akb_worker")


def downgrade():
    raise RuntimeError("Retain device/source metadata on application rollback; no destructive downgrade")
