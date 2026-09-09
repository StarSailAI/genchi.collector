"""Passwordless email challenges, account profiles and broader subscriptions."""

from alembic import op

revision = "0008_email_code_auth"
down_revision = "0007_inbound_mail"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
ALTER TABLE genchi_private.accounts
 ADD COLUMN display_name TEXT NOT NULL DEFAULT '' CHECK(length(display_name)<=60),
 ADD COLUMN disabled_at TIMESTAMPTZ,
 ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 ADD COLUMN last_login_at TIMESTAMPTZ;
CREATE TABLE genchi_private.email_challenges (
 email TEXT PRIMARY KEY,
 id TEXT NOT NULL UNIQUE,
 purpose TEXT NOT NULL DEFAULT 'login' CHECK(purpose='login'),
 code_digest TEXT NOT NULL,
 timezone TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5),
 status TEXT NOT NULL CHECK(status IN ('PENDING','SENT','CONSUMED','FAILED')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 expires_at TIMESTAMPTZ NOT NULL,
 sent_at TIMESTAMPTZ,
 consumed_at TIMESTAMPTZ
);
CREATE INDEX email_challenges_expiry ON genchi_private.email_challenges(expires_at);
CREATE INDEX sessions_account ON genchi_private.sessions(account_id);
CREATE INDEX sessions_expiry ON genchi_private.sessions(expires_at);
ALTER TABLE genchi_private.follows DROP CONSTRAINT follows_target_type_check;
ALTER TABLE genchi_private.follows ADD CONSTRAINT follows_target_type_check
 CHECK(target_type IN ('SUBJECT','ACTIVITY','KEYWORD','TAG'));
REVOKE ALL ON genchi_private.email_challenges FROM PUBLIC;
-- Retire link authentication without deleting accounts or existing sessions.
UPDATE genchi_private.mail_queue SET status='CANCELED',payload='{}',last_error=NULL
 WHERE kind='LOGIN';
DELETE FROM genchi_private.login_tokens;
UPDATE "SchemaContract" SET minor=5,"updatedAt"=NOW() WHERE id=1;
""")


def downgrade():
    raise RuntimeError("Preserve user accounts and subscriptions; roll back applications instead.")
