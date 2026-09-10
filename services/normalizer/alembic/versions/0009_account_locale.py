"""Persist explicit account and email challenge languages."""

from alembic import op

revision = "0009_account_locale"
down_revision = "0008_email_code_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
ALTER TABLE genchi_private.accounts ADD COLUMN locale TEXT NOT NULL DEFAULT 'zh-Hans'
 CHECK(locale IN ('zh-Hans','zh-Hant','en','ja'));
ALTER TABLE genchi_private.email_challenges ADD COLUMN locale TEXT NOT NULL DEFAULT 'zh-Hans'
 CHECK(locale IN ('zh-Hans','zh-Hant','en','ja'));
UPDATE "SchemaContract" SET minor=6,"updatedAt"=NOW() WHERE id=1;
""")


def downgrade():
    raise RuntimeError("Preserve account preferences; roll back applications instead.")
