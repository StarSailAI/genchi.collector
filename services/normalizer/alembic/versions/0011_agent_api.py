"""Personal API keys and privacy-safe Agent request audit."""

from alembic import op

revision = "0011_agent_api"
down_revision = "0010_collection_digest"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE genchi_private.api_keys (
 id TEXT PRIMARY KEY,
 account_id TEXT NOT NULL REFERENCES genchi_private.accounts(id) ON DELETE CASCADE,
 name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 60),
 prefix TEXT NOT NULL,
 secret_digest CHAR(64) NOT NULL UNIQUE,
 scopes JSONB NOT NULL DEFAULT '[]',
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 expires_at TIMESTAMPTZ,
 last_used_at TIMESTAMPTZ,
 revoked_at TIMESTAMPTZ
);
CREATE INDEX api_keys_account ON genchi_private.api_keys(account_id,created_at DESC);
CREATE TABLE genchi_private.api_key_requests (
 id BIGSERIAL PRIMARY KEY,
 request_id UUID NOT NULL UNIQUE,
 api_key_id TEXT NOT NULL REFERENCES genchi_private.api_keys(id) ON DELETE RESTRICT,
 account_id TEXT NOT NULL REFERENCES genchi_private.accounts(id) ON DELETE CASCADE,
 method TEXT NOT NULL,
 route TEXT NOT NULL,
 status_code INTEGER NOT NULL,
 elapsed_ms INTEGER NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX api_key_requests_account ON genchi_private.api_key_requests(account_id,created_at DESC);
REVOKE ALL ON genchi_private.api_keys FROM PUBLIC;
REVOKE ALL ON genchi_private.api_key_requests FROM PUBLIC;
UPDATE "SchemaContract" SET minor=8,"updatedAt"=NOW() WHERE id=1;
""")


def downgrade():
    raise RuntimeError(
        "Keep API key revocation and audit history; roll back the application instead."
    )
