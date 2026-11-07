"""Fence engine bindings and persist external WorkBuddy jobs, without moving user data."""
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE memory_bindings ADD COLUMN engine VARCHAR(20) NOT NULL DEFAULT 'tencent';
    CREATE TABLE agent_jobs (
      id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES tenants(id),
      source_id VARCHAR(36) NOT NULL, source_version VARCHAR(64) NOT NULL,
      state VARCHAR(20) NOT NULL, credential_id VARCHAR(36), lease_digest VARCHAR(64),
      lease_until TIMESTAMPTZ, attempts INTEGER NOT NULL, completion_hash VARCHAR(64),
      result_ids JSON NOT NULL, error VARCHAR(40), created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      UNIQUE(tenant_id, source_id, source_version));
    CREATE INDEX ix_agent_jobs_tenant_id ON agent_jobs(tenant_id);
    ALTER TABLE agent_jobs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE agent_jobs FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_scope ON agent_jobs USING (tenant_id = current_setting('akb.tenant_id',true))
      WITH CHECK (tenant_id = current_setting('akb.tenant_id',true));
    GRANT SELECT, INSERT, UPDATE, DELETE ON agent_jobs TO akb_app, akb_worker;
    """)


def downgrade():
    raise RuntimeError("Keep external job history and engine binding; rollback requires compatible code or a verified backup")
