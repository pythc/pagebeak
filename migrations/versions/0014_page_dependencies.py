"""Generated pages carry the knowledge revision they were prepared against."""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.pages ADD COLUMN snapshot_revision INTEGER")
    op.execute("ALTER TABLE public.pages ADD COLUMN fact_ids JSON NOT NULL DEFAULT '[]'::json")
    op.execute("ALTER TABLE public.pages ALTER COLUMN fact_ids DROP DEFAULT")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; use a reviewed forward migration.")
