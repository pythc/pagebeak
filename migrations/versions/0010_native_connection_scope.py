"""Require native file-to-connection references to remain inside the same tenant."""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE public.native_connections ADD CONSTRAINT uq_native_connection_tenant_id UNIQUE(tenant_id,id)"
    )
    op.execute("ALTER TABLE public.native_files DROP CONSTRAINT native_files_connection_id_fkey")
    op.execute("""ALTER TABLE public.native_files ADD CONSTRAINT fk_native_file_connection
        FOREIGN KEY(tenant_id,connection_id) REFERENCES public.native_connections(tenant_id,id)""")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; use a reviewed forward migration.")
