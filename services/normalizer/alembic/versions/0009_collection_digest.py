"""Durable daily collection digest for the administrator."""

from alembic import op

revision = "0009_collection_digest"
down_revision = "0008_email_code_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE genchi_private.collection_digest_state (
 id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(id),
 last_version_id BIGINT NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO genchi_private.collection_digest_state(id,last_version_id)
 VALUES(TRUE,(SELECT COALESCE(max(id),0) FROM allfeeds.resource_versions));
CREATE TABLE genchi_private.collection_digests (
 digest_date DATE PRIMARY KEY,
 window_start_id BIGINT NOT NULL,
 window_end_id BIGINT NOT NULL CHECK(window_end_id>=window_start_id),
 status TEXT NOT NULL CHECK(status IN ('PENDING','SENDING','SENT','EMPTY','FAILED','UNCERTAIN')),
 counts JSONB NOT NULL DEFAULT '{}',
 payload JSONB,
 attempts INTEGER NOT NULL DEFAULT 0,
 available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 first_send_at TIMESTAMPTZ,
 locked_at TIMESTAMPTZ,
 sent_at TIMESTAMPTZ,
 resend_email_id UUID,
 last_error TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX collection_digests_pending ON genchi_private.collection_digests(available_at)
 WHERE status='PENDING';
REVOKE ALL ON genchi_private.collection_digest_state FROM PUBLIC;
REVOKE ALL ON genchi_private.collection_digests FROM PUBLIC;
UPDATE "SchemaContract" SET minor=6,"updatedAt"=NOW() WHERE id=1;
""")


def downgrade():
    raise RuntimeError(
        "Keep collection digest delivery records; roll back the application instead."
    )
