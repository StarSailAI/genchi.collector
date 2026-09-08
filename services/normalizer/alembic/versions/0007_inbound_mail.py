"""Durable, private Resend incoming mail forwarding queue."""

from alembic import op

revision = "0007_inbound_mail"
down_revision = "0006_catalog_names"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE genchi_private.inbound_mail (
 email_id UUID PRIMARY KEY,
 status TEXT NOT NULL DEFAULT 'PENDING'
   CHECK(status IN ('PENDING','SENT','SKIPPED','FAILED','UNCERTAIN')),
 attempts INTEGER NOT NULL DEFAULT 0,
 available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 first_send_at TIMESTAMPTZ,
 sent_at TIMESTAMPTZ,
 forwarded_id UUID,
 payload JSONB,
 last_error TEXT
);
CREATE INDEX inbound_mail_pending ON genchi_private.inbound_mail(available_at)
 WHERE status='PENDING';
REVOKE ALL ON genchi_private.inbound_mail FROM PUBLIC;
CREATE TABLE genchi_private.inbound_cursor (
 id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(id), last_email_id UUID
);
REVOKE ALL ON genchi_private.inbound_cursor FROM PUBLIC;
UPDATE "SchemaContract" SET minor=4,"updatedAt"=NOW() WHERE id=1;
""")


def downgrade():
    raise RuntimeError("Keep incoming mail delivery records; roll back the application instead.")
