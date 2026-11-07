"""Allow runtime startup to read, but never modify, the schema version."""
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("GRANT SELECT ON public.alembic_version TO akb_app, akb_worker")


def downgrade():
    op.execute("REVOKE SELECT ON public.alembic_version FROM akb_app, akb_worker")
