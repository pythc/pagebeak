"""Versioned provenance, output hashes and derived storage accounting."""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE tenants ADD COLUMN memory_bytes BIGINT NOT NULL DEFAULT 0;
    ALTER TABLE memory_objects ADD COLUMN citations JSON NOT NULL DEFAULT '[]';
    ALTER TABLE memory_objects ALTER COLUMN citations DROP DEFAULT;
    CREATE TABLE filling_receipts (
      id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES tenants(id),
      request_id VARCHAR(36) NOT NULL, request_hash VARCHAR(64) NOT NULL,
      value_hashes JSON NOT NULL, source_versions JSON NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
      UNIQUE(tenant_id, request_id));
    CREATE INDEX ix_filling_receipts_tenant_id ON filling_receipts(tenant_id);
    ALTER TABLE filling_receipts ENABLE ROW LEVEL SECURITY;
    ALTER TABLE filling_receipts FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_scope ON filling_receipts
      USING (tenant_id = current_setting('akb.tenant_id',true))
      WITH CHECK (tenant_id = current_setting('akb.tenant_id',true));
    GRANT SELECT, INSERT, UPDATE, DELETE ON filling_receipts TO akb_app, akb_worker;
    CREATE OR REPLACE FUNCTION public.akb_memory_maintenance_tenants() RETURNS TABLE(tenant_id varchar)
    LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      SELECT tenant_id FROM public.memory_bindings
      UNION SELECT tenant_id FROM public.memory_garbage
    $$;
    """)


def downgrade():
    raise RuntimeError("Application rollback retains evidence, receipts and memory accounting; destructive downgrade disabled")
