"""A bounded worker-only tenant discovery function for memory cleanup."""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE FUNCTION public.akb_memory_maintenance_tenants() RETURNS TABLE(tenant_id varchar)
    LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
      SELECT tenant_id FROM public.memory_garbage
      UNION SELECT tenant_id FROM public.memory_objects
        WHERE NOT deleted AND expires_at IS NOT NULL AND expires_at <= now()
    $$;
    REVOKE ALL ON FUNCTION public.akb_memory_maintenance_tenants() FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.akb_memory_maintenance_tenants() TO akb_worker;
    """)


def downgrade():
    op.execute("DROP FUNCTION public.akb_memory_maintenance_tenants()")
