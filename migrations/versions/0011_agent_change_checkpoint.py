"""Keep an agent's acknowledgement independent of the background indexing checkpoint."""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.credentials ADD COLUMN change_cursor BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE public.credentials ADD COLUMN observed_hash VARCHAR(64) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE public.credentials ALTER COLUMN change_cursor DROP DEFAULT")
    op.execute("ALTER TABLE public.credentials ALTER COLUMN observed_hash DROP DEFAULT")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; use a reviewed forward migration.")
