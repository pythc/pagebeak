"""Stable federated users; additive migration, existing spaces remain untouched."""
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE external_identities (
      id VARCHAR(36) PRIMARY KEY, issuer VARCHAR(512) NOT NULL,
      namespace VARCHAR(200) NOT NULL, subject VARCHAR(255) NOT NULL,
      user_id VARCHAR(36) NOT NULL REFERENCES users(id),
      provisions_space BOOLEAN NOT NULL DEFAULT false,
      created_at TIMESTAMPTZ NOT NULL, last_login_at TIMESTAMPTZ NOT NULL,
      UNIQUE (issuer, namespace, subject));
    CREATE INDEX ix_external_identities_user_id ON external_identities(user_id);
    CREATE TABLE identity_logins (
      id VARCHAR(36) PRIMARY KEY, digest VARCHAR(64) UNIQUE NOT NULL,
      device_id VARCHAR(36) NOT NULL REFERENCES space_devices(id),
      provider VARCHAR(80) NOT NULL, configuration VARCHAR(64) NOT NULL,
      nonce_digest VARCHAR(64) NOT NULL, verifier VARCHAR(100) NOT NULL,
      intent VARCHAR(16) NOT NULL, user_id VARCHAR(36) REFERENCES users(id),
      reference VARCHAR(100), expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ);
    GRANT SELECT, INSERT, UPDATE, DELETE ON external_identities, identity_logins TO akb_app;
    """)


def downgrade():
    raise RuntimeError("保留账号绑定及资料归属。先关闭身份接入、备份并评审回退；不能删除绑定表。")
