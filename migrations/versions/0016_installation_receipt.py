"""Separate installation receipt from actual assistant connection verification."""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.onboarding ADD COLUMN installation JSON NOT NULL DEFAULT '{}'::json")
    op.execute("ALTER TABLE public.onboarding ALTER COLUMN installation DROP DEFAULT")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; use a reviewed forward migration.")
