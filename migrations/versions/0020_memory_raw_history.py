"""Keep raw versions referenced by protected or stale pages during reconciliation."""
from alembic import op

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE memory_objects ADD COLUMN raw_refs JSON NOT NULL DEFAULT '[]';
    UPDATE memory_objects o SET raw_refs = COALESCE((
      SELECT json_agg(f.value) FROM memory_bindings b, json_each(b.exports) e,
      json_array_elements_text(e.value->'files') f
      WHERE b.tenant_id = o.tenant_id AND o.source_versions::jsonb ? e.key
    ), '[]'::json);
    """)


def downgrade():
    raise RuntimeError('Keep raw-history metadata on application rollback')
