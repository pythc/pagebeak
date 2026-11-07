"""Scoped expiring transfers for WorkBuddy's bundled Skill scripts."""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE host_transfers (
      id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES tenants(id),
      request_id VARCHAR(36) NOT NULL, kind VARCHAR(16) NOT NULL,
      credential_id VARCHAR(36) NOT NULL REFERENCES credentials(id), digest VARCHAR(64) NOT NULL,
      expires_at TIMESTAMPTZ NOT NULL, upload_id VARCHAR(36), spec JSON NOT NULL, result JSON,
      UNIQUE(tenant_id,request_id));
    CREATE INDEX ix_host_transfers_tenant_id ON host_transfers(tenant_id);
    ALTER TABLE host_transfers ENABLE ROW LEVEL SECURITY;
    ALTER TABLE host_transfers FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_scope ON host_transfers USING (tenant_id=current_setting('akb.tenant_id',true))
      WITH CHECK (tenant_id=current_setting('akb.tenant_id',true));
    GRANT SELECT,INSERT,UPDATE,DELETE ON host_transfers TO akb_app,akb_worker;
    """)


def downgrade():
    raise RuntimeError("Preserve transfer/retry history; use compatible code or restore a verified backup")
