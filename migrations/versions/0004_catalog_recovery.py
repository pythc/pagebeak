"""Refresh directories when ingestion leases exhaust their last retry."""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    # Exhaustion is a source status change and must also refresh an enabled directory.
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
        RETURNING s.id, s.tenant_id
      ), catalog_refresh AS (
        INSERT INTO public.jobs (id, tenant_id, source_id, kind, source_revision, status,
                                 fencing_token, attempts, created_at, result)
        SELECT gen_random_uuid()::text, c.tenant_id, NULL, 'organize', 0, 'queued',
               0, 0, clock_timestamp(), '{}'::json
        FROM public.catalogs c WHERE c.enabled
          AND c.tenant_id IN (SELECT tenant_id FROM failed_sources)
          AND NOT EXISTS (SELECT 1 FROM public.jobs q WHERE q.tenant_id = c.tenant_id
              AND q.kind = 'organize' AND q.status IN ('queued', 'running') AND q.attempts < 5)
        RETURNING id
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
