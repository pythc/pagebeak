"""Device-bound automatic spaces; preserve all existing account bindings."""
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE space_devices (
      id VARCHAR(36) PRIMARY KEY, digest VARCHAR(64) UNIQUE NOT NULL,
      csrf VARCHAR(64) NOT NULL, user_id VARCHAR(36) REFERENCES users(id),
      created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
      revoked BOOLEAN NOT NULL DEFAULT false);
    CREATE TABLE space_links (
      id VARCHAR(36) PRIMARY KEY, digest VARCHAR(64) UNIQUE NOT NULL,
      user_id VARCHAR(36) NOT NULL REFERENCES users(id),
      credential_id VARCHAR(36) NOT NULL REFERENCES credentials(id),
      expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ);
    GRANT SELECT, INSERT, UPDATE, DELETE ON space_devices, space_links TO akb_app;
    """)


def downgrade():
    raise RuntimeError("Retain device bindings and user data; disable passwordless entry before a reviewed rollback")
