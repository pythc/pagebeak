"""Personal file folders and restorable trash, retaining stable source identities."""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE public.file_folders (
        id VARCHAR(36) PRIMARY KEY,
        tenant_id VARCHAR(36) NOT NULL REFERENCES public.tenants(id),
        parent_id VARCHAR(36), name VARCHAR(255) NOT NULL, created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (tenant_id, id),
        FOREIGN KEY (tenant_id, parent_id) REFERENCES public.file_folders(tenant_id, id))""")
    op.execute("CREATE INDEX ix_file_folders_tenant_id ON public.file_folders(tenant_id)")
    op.execute("ALTER TABLE public.file_folders ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.file_folders FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_scope ON public.file_folders USING
        (tenant_id = current_setting('akb.tenant_id', true)) WITH CHECK
        (tenant_id = current_setting('akb.tenant_id', true))""")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON public.file_folders TO akb_app, akb_worker")
    op.execute("ALTER TABLE public.sources ADD COLUMN folder_id VARCHAR(36)")
    op.execute("ALTER TABLE public.sources ADD COLUMN trashed_at TIMESTAMPTZ")
    op.execute("""ALTER TABLE public.sources ADD CONSTRAINT fk_sources_folder
        FOREIGN KEY (tenant_id, folder_id) REFERENCES public.file_folders(tenant_id, id)""")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; use a reviewed forward migration.")
