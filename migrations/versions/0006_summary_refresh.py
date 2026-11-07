"""Refresh automatic catalogs after changing the extractive summary algorithm."""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""INSERT INTO public.jobs
        (id, tenant_id, source_id, kind, source_revision, status, fencing_token, attempts, created_at, result)
        SELECT gen_random_uuid()::text, c.tenant_id, NULL, 'organize', 0, 'queued', 0, 0, clock_timestamp(), '{}'::json
        FROM public.catalogs c WHERE c.enabled AND NOT EXISTS
        (SELECT 1 FROM public.jobs j WHERE j.tenant_id=c.tenant_id AND j.kind='organize'
          AND j.status IN ('queued','running'))""")


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; use a reviewed forward migration.")
