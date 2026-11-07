"""Resumable onboarding and short-lived single-use installation tickets."""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE onboarding (
        id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES tenants(id),
        revision INTEGER NOT NULL, answers JSON NOT NULL, skipped JSON NOT NULL,
        dismissed BOOLEAN NOT NULL, credential_id VARCHAR(36) REFERENCES credentials(id),
        started_at TIMESTAMP WITH TIME ZONE, completed_at TIMESTAMP WITH TIME ZONE,
        UNIQUE (tenant_id))""")
    op.execute("""CREATE TABLE connection_tickets (
        id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES tenants(id),
        digest VARCHAR(64) NOT NULL UNIQUE, issued_by VARCHAR(36) NOT NULL REFERENCES credentials(id),
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL, used_at TIMESTAMP WITH TIME ZONE)""")
    for table in ["onboarding", "connection_tickets"]:
        op.execute(f"CREATE INDEX ix_{table}_tenant_id ON {table} (tenant_id)")
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON public.{table} USING (tenant_id = current_setting('akb.tenant_id', true)) WITH CHECK (tenant_id = current_setting('akb.tenant_id', true))")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO akb_app, akb_worker")
    op.execute("""INSERT INTO onboarding (id, tenant_id, revision, answers, skipped, dismissed)
        SELECT gen_random_uuid()::text, id, 1, '{}'::json, '[]'::json, false FROM tenants""")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; use a reviewed forward migration.")
