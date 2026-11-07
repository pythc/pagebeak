"""Stable shared catalog, protected user overrides and durable organization jobs."""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.jobs ALTER COLUMN source_id DROP NOT NULL")
    op.execute("ALTER TABLE public.jobs ADD COLUMN result JSON NOT NULL DEFAULT '{}'::json")
    op.execute("ALTER TABLE public.jobs ALTER COLUMN result DROP DEFAULT")
    op.execute("""
    CREATE TABLE public.catalogs (
      id VARCHAR(36) PRIMARY KEY,
      tenant_id VARCHAR(36) NOT NULL UNIQUE REFERENCES public.tenants(id),
      revision INTEGER NOT NULL,
      enabled BOOLEAN NOT NULL,
      manual JSON NOT NULL,
      last_result JSON NOT NULL,
      organized_at TIMESTAMPTZ
    )
    """)
    op.execute("""
    CREATE TABLE public.catalog_entries (
      id VARCHAR(36) PRIMARY KEY,
      tenant_id VARCHAR(36) NOT NULL REFERENCES public.tenants(id),
      source_id VARCHAR(36) NOT NULL,
      fingerprint VARCHAR(64) NOT NULL,
      generated JSON NOT NULL,
      manual JSON NOT NULL,
      generated_at TIMESTAMPTZ NOT NULL,
      UNIQUE (tenant_id, source_id),
      FOREIGN KEY (tenant_id, source_id) REFERENCES public.sources(tenant_id, id)
    )
    """)
    for table in ["catalogs", "catalog_entries"]:
        op.execute(f"CREATE INDEX ix_{table}_tenant_id ON public.{table}(tenant_id)")
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_scope ON public.{table} USING "
            "(tenant_id = current_setting('akb.tenant_id', true)) WITH CHECK "
            "(tenant_id = current_setting('akb.tenant_id', true))"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO akb_app, akb_worker")
    op.execute("CREATE INDEX ix_catalog_entries_source_id ON public.catalog_entries(source_id)")
    # Existing libraries are backfilled by the normal worker after upgrade, not inside a migration.
    op.execute("""
    INSERT INTO public.catalogs (id, tenant_id, revision, enabled, manual, last_result)
      SELECT gen_random_uuid()::text, id, 1, true, '{}'::json, '{}'::json FROM public.tenants
    """)
    op.execute("""
    INSERT INTO public.jobs (id, tenant_id, source_id, kind, source_revision, status,
                            fencing_token, attempts, created_at, result)
      SELECT gen_random_uuid()::text, id, NULL, 'organize', 0, 'queued',
             0, 0, clock_timestamp(), '{}'::json FROM public.tenants
    """)


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; use a reviewed forward migration.")
