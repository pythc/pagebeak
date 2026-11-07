"""Public assistant registration and explicit per-user OAuth authorization."""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    # Like credentials, these records must be located before a tenant is known.
    # Tenant data continues to use its existing forced RLS and server-derived ID.
    op.execute("""
    CREATE TABLE assistant_clients (
      id VARCHAR(200) PRIMARY KEY, metadata_json JSON NOT NULL);
    CREATE TABLE assistant_grants (
      id VARCHAR(36) PRIMARY KEY, kind VARCHAR(16) NOT NULL,
      digest VARCHAR(64) NOT NULL UNIQUE,
      client_id VARCHAR(200) NOT NULL REFERENCES assistant_clients(id),
      user_id VARCHAR(36) REFERENCES users(id),
      credential_id VARCHAR(36) REFERENCES credentials(id),
      parameters JSON NOT NULL, expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ);
    GRANT SELECT, INSERT, UPDATE, DELETE ON assistant_clients, assistant_grants TO akb_app;
    """)


def downgrade():
    raise RuntimeError("Revoke assistant grants before rollback; retain authorization history")
