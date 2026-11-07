"""Durable native file associations, history, and indexed version checkpoints."""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.sources ADD COLUMN indexed_hash VARCHAR(64) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE public.sources ALTER COLUMN indexed_hash DROP DEFAULT")
    op.execute("ALTER TABLE public.sources ADD COLUMN origin_path VARCHAR(2048)")
    op.execute("""CREATE TABLE public.native_connections (
        id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES public.tenants(id),
        provider_user_id VARCHAR(80) NOT NULL UNIQUE, enabled BOOLEAN NOT NULL,
        last_scan_at TIMESTAMPTZ, error TEXT, UNIQUE(tenant_id))""")
    op.execute("""CREATE TABLE public.native_link_codes (
        id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES public.tenants(id),
        digest VARCHAR(64) NOT NULL UNIQUE, expires_at TIMESTAMPTZ NOT NULL, consumed BOOLEAN NOT NULL)""")
    op.execute("""CREATE TABLE public.native_files (
        id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES public.tenants(id),
        provider_file_id VARCHAR(80) NOT NULL,
        connection_id VARCHAR(36) NOT NULL REFERENCES public.native_connections(id),
        source_id VARCHAR(36), snapshot JSON NOT NULL, missing_count INTEGER NOT NULL,
        UNIQUE(tenant_id, provider_file_id),
        FOREIGN KEY(tenant_id, source_id) REFERENCES public.sources(tenant_id, id))""")
    op.execute("""CREATE TABLE public.file_changes (
        id BIGSERIAL PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES public.tenants(id),
        source_id VARCHAR(36), file_id VARCHAR(120) NOT NULL, action VARCHAR(40) NOT NULL,
        actor VARCHAR(24) NOT NULL, before JSON NOT NULL, after JSON NOT NULL, created_at TIMESTAMPTZ NOT NULL)""")
    for table in ["native_connections", "native_link_codes", "native_files", "file_changes"]:
        op.execute(f"CREATE INDEX ix_{table}_tenant_id ON public.{table}(tenant_id)")
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_scope ON public.{table} USING
            (tenant_id = current_setting('akb.tenant_id', true)) WITH CHECK
            (tenant_id = current_setting('akb.tenant_id', true))""")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO akb_app, akb_worker")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE public.file_changes_id_seq TO akb_app, akb_worker")
    # Resolve only a proven provider identity / an unguessable, short-lived linking capability.
    for name, arg, query in [
        (
            "akb_native_tenant",
            "provider_id TEXT",
            "SELECT tenant_id FROM public.native_connections WHERE provider_user_id=provider_id AND enabled",
        ),
        (
            "akb_native_code_tenant",
            "code_digest TEXT",
            "SELECT tenant_id FROM public.native_link_codes WHERE digest=code_digest AND NOT consumed AND expires_at>now()",
        ),
    ]:
        op.execute(f"""CREATE FUNCTION public.{name}({arg}) RETURNS TEXT
            LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$ {query} $$""")
        op.execute(f"REVOKE ALL ON FUNCTION public.{name}(TEXT) FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{name}(TEXT) TO akb_app, akb_worker")
    op.execute("""CREATE FUNCTION public.akb_native_tenants() RETURNS TABLE(tenant_id TEXT)
        LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT tenant_id::TEXT FROM public.native_connections WHERE enabled $$""")
    op.execute("REVOKE ALL ON FUNCTION public.akb_native_tenants() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.akb_native_tenants() TO akb_worker")
    op.execute("""INSERT INTO public.jobs
        (id, tenant_id, source_id, kind, source_revision, status, fencing_token, attempts, created_at, result)
        SELECT gen_random_uuid()::text, c.tenant_id, NULL, 'organize', 0, 'queued', 0, 0, clock_timestamp(), '{}'::json
        FROM public.catalogs c WHERE c.enabled AND NOT EXISTS
        (SELECT 1 FROM public.jobs j WHERE j.tenant_id=c.tenant_id AND j.kind='organize'
          AND j.status IN ('queued','running'))""")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; use a reviewed forward migration.")
