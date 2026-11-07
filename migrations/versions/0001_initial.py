"""Initial business schema, row policies and narrowly scoped queue dispatcher."""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    ddl = (Path(__file__).resolve().parents[1] / "schema_0001.sql").read_text()
    ddl = "\n".join(line for line in ddl.splitlines() if not line.lstrip().startswith("--"))
    for statement in ddl.split(";"):
        if statement.strip():
            op.execute(statement)
    TENANT_TABLES = [
        "sources",
        "evidence",
        "claims",
        "decisions",
        "jobs",
        "pages",
        "form_drafts",
        "idempotency",
        "audit",
    ]
    for table in ["tenants", *TENANT_TABLES]:
        column = "id" if table == "tenants" else "tenant_id"
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_scope ON public.{table} USING "
            f"({column} = current_setting('akb.tenant_id', true)) WITH CHECK "
            f"({column} = current_setting('akb.tenant_id', true))"
        )
    op.execute("GRANT USAGE ON SCHEMA public TO akb_app, akb_worker")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO akb_app, akb_worker")
    op.execute("REVOKE ALL ON public.alembic_version FROM akb_app, akb_worker")
    # Only the worker can invoke this fixed control-plane operation. No document body is returned.
    op.execute("""
    CREATE FUNCTION public.akb_claim_job(p_worker text, p_lease integer)
    RETURNS TABLE(job_id text, tenant_id text, fencing_token integer)
    LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
    AS $$
      UPDATE public.jobs AS j SET status = 'running', worker_id = p_worker,
        attempts = j.attempts + 1, fencing_token = j.fencing_token + 1,
        lease_until = clock_timestamp() + make_interval(secs => LEAST(GREATEST(p_lease, 5), 600))
      WHERE j.id = (
        SELECT q.id FROM public.jobs q
        WHERE q.attempts < 5 AND (q.status = 'queued' OR
          (q.status = 'running' AND q.lease_until < clock_timestamp()))
        ORDER BY q.created_at, q.id FOR UPDATE SKIP LOCKED LIMIT 1
      ) RETURNING j.id::text, j.tenant_id::text, j.fencing_token
    $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.akb_claim_job(text, integer) FROM PUBLIC, akb_app")
    op.execute("GRANT EXECUTE ON FUNCTION public.akb_claim_job(text, integer) TO akb_worker")


def downgrade():
    raise RuntimeError(
        "Destructive downgrade is disabled. Restore only in an explicitly isolated environment."
    )
