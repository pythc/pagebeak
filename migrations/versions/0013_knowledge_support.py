"""Retain source version, subject evidence and effective-date quotes."""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.knowledge_facts ADD COLUMN support JSON NOT NULL DEFAULT '{}'::json")
    op.execute("ALTER TABLE public.knowledge_facts ALTER COLUMN support DROP DEFAULT")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; use a reviewed forward migration.")
