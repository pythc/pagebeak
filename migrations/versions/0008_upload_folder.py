"""Retain the selected destination across chunk upload completion."""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.uploads ADD COLUMN folder_id VARCHAR(36)")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; use a reviewed forward migration.")
