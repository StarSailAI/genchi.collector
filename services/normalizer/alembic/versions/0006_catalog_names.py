"""Separate source names from reviewed Chinese display names; retain a naming audit."""

from alembic import op

revision = "0006_catalog_names"
down_revision = "0005_scoped_source_keys"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
ALTER TABLE catalog_milestones ADD COLUMN title_zh TEXT;
ALTER TABLE catalog_occurrences ADD COLUMN label_zh TEXT;
CREATE TABLE catalog_names (
 entity_type TEXT NOT NULL CHECK(entity_type IN ('ACTIVITY','MILESTONE','OCCURRENCE','SUBJECT')),
 entity_id TEXT NOT NULL, source_text TEXT NOT NULL, display_text TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('NORMALIZED','REVIEW')),
 method TEXT NOT NULL, policy_version TEXT NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(entity_type,entity_id)
);
CREATE INDEX catalog_names_review ON catalog_names(state,entity_type);
CREATE TABLE catalog_name_history (
 id BIGSERIAL PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
 source_text TEXT NOT NULL, before_text TEXT, after_text TEXT NOT NULL,
 state TEXT NOT NULL, method TEXT NOT NULL, policy_version TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX catalog_name_history_entity ON catalog_name_history(entity_type,entity_id,id DESC);
UPDATE "SchemaContract" SET minor=3,"updatedAt"=NOW() WHERE id=1;
""")


def downgrade():
    raise RuntimeError(
        "Name history and original names must be retained; roll back the application."
    )
