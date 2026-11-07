"""Quota accounting, resumable uploads and explicitly authorized cleanup."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE public.users ALTER COLUMN is_admin DROP DEFAULT")
    op.execute("ALTER TABLE public.sources ALTER COLUMN size TYPE BIGINT")
    for column, default in [("quota_bytes", 10 * 1024**3), ("used_bytes", 0), ("reserved_bytes", 0)]:
        op.execute(f"ALTER TABLE public.tenants ADD COLUMN {column} BIGINT NOT NULL DEFAULT {default}")
        op.execute(f"ALTER TABLE public.tenants ALTER COLUMN {column} DROP DEFAULT")
    # Revoked originals still occupy space until their cleanup worker unlinks them.
    op.execute("""UPDATE public.tenants t SET used_bytes = COALESCE(
        (SELECT SUM(s.size) FROM public.sources s WHERE s.tenant_id=t.id AND s.object_key<>''), 0)""")
    op.execute("""CREATE TABLE public.uploads (
        id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES public.tenants(id),
        filename VARCHAR(255) NOT NULL, media_type VARCHAR(120) NOT NULL,
        subject_scope VARCHAR(16) NOT NULL, size BIGINT NOT NULL, "offset" BIGINT NOT NULL,
        status VARCHAR(20) NOT NULL, source_id VARCHAR(36), expires_at TIMESTAMPTZ NOT NULL)""")
    op.execute("""CREATE TABLE public.cleanup_plans (
        id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES public.tenants(id),
        mode VARCHAR(24) NOT NULL, status VARCHAR(24) NOT NULL, manifest JSON NOT NULL,
        summary TEXT NOT NULL, authorizer_id VARCHAR(36), result JSON NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL)""")
    op.execute("""CREATE TABLE public.storage_archives (
        id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL REFERENCES public.tenants(id),
        title VARCHAR(200) NOT NULL, content TEXT NOT NULL, manifest JSON NOT NULL,
        plan_id VARCHAR(36) NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL)""")
    for table in ["uploads", "cleanup_plans", "storage_archives"]:
        op.execute(f"CREATE INDEX ix_{table}_tenant_id ON public.{table}(tenant_id)")
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_scope ON public.{table}
            USING (tenant_id=current_setting('akb.tenant_id', true))
            WITH CHECK (tenant_id=current_setting('akb.tenant_id', true))""")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO akb_app, akb_worker")
    # The API role can manage credentials but cannot promote users, including via raw SQL.
    op.execute("REVOKE INSERT, UPDATE ON public.users FROM akb_app, akb_worker")
    op.execute("GRANT INSERT (id,email,display_name,password_hash,tenant_id) ON public.users TO akb_app")
    # ORM inserts an explicit false; a column grant would let raw SQL insert true. Enforce it by trigger.
    op.execute("GRANT INSERT (is_admin) ON public.users TO akb_app")
    op.execute("""CREATE FUNCTION public.akb_guard_admin() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF current_user IN ('akb_app','akb_worker') AND NEW.is_admin THEN
          RAISE EXCEPTION 'admin promotion requires operator';
        END IF; RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER guard_admin BEFORE INSERT ON public.users FOR EACH ROW EXECUTE FUNCTION public.akb_guard_admin()"
    )
    op.execute("""CREATE FUNCTION public.akb_staging_tenants() RETURNS TABLE(tenant_id text)
        LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
        SELECT DISTINCT u.tenant_id::text FROM public.uploads u WHERE u.expires_at < clock_timestamp()
          AND u.status IN ('staging','completed') $$""")
    op.execute("REVOKE ALL ON FUNCTION public.akb_staging_tenants() FROM PUBLIC, akb_app")
    op.execute("GRANT EXECUTE ON FUNCTION public.akb_staging_tenants() TO akb_worker")


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; use a reviewed forward migration.")
