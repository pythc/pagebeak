"""Bound retry eligibility and finalize jobs abandoned after the last allowed attempt."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE OR REPLACE FUNCTION public.akb_claim_job(p_worker text, p_lease integer)
    RETURNS TABLE(job_id text, tenant_id text, fencing_token integer)
    LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
    AS $$
      WITH exhausted AS (
        UPDATE public.jobs AS e SET status = 'failed', error = 'retry_exhausted'
        WHERE e.status = 'running' AND e.attempts >= 5 AND e.lease_until < clock_timestamp()
        RETURNING e.source_id, e.tenant_id, e.kind, e.source_revision
      ), failed_sources AS (
        UPDATE public.sources AS s SET status = 'failed', error = '任务重试次数耗尽，请检查后台服务'
        FROM exhausted e WHERE e.kind = 'ingest' AND s.id = e.source_id
          AND s.tenant_id = e.tenant_id AND s.revision = e.source_revision AND s.revoked_at IS NULL
        RETURNING s.id
      )
      UPDATE public.jobs AS j SET status = 'running', worker_id = p_worker,
        attempts = j.attempts + 1, fencing_token = j.fencing_token + 1,
        lease_until = clock_timestamp() + make_interval(secs => LEAST(GREATEST(p_lease, 5), 600))
      WHERE j.id = (
        SELECT q.id FROM public.jobs q WHERE q.attempts < 5 AND (
          (q.status = 'queued' AND (q.lease_until IS NULL OR q.lease_until <= clock_timestamp())) OR
          (q.status = 'running' AND q.lease_until < clock_timestamp()))
        ORDER BY q.created_at, q.id FOR UPDATE SKIP LOCKED LIMIT 1
      ) RETURNING j.id::text, j.tenant_id::text, j.fencing_token
    $$
    """)


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; use a reviewed forward migration.")
