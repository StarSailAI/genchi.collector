"""Explain and verify activity-to-subject associations."""

from alembic import op

revision = "0012_subject_provenance"
down_revision = "0011_agent_api"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
ALTER TABLE catalog_activity_subjects
  ADD COLUMN relation_kind TEXT NOT NULL DEFAULT 'LEGACY'
    CHECK(relation_kind IN ('LEGACY','DIRECT','PERFORMER','COLLABORATION','CAST','SOURCE_SCOPE')),
  ADD COLUMN participant_name TEXT,
  ADD COLUMN scope_note TEXT,
  ADD COLUMN evidence_id TEXT REFERENCES catalog_evidence(id),
  ADD COLUMN verified BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX catalog_activity_subject_verified
  ON catalog_activity_subjects(subject_slug,verified,activity_id);
ALTER TABLE catalog_activity_subjects ALTER COLUMN relation_kind SET DEFAULT 'SOURCE_SCOPE';
UPDATE "SchemaContract" SET minor=9,"updatedAt"=NOW() WHERE id=1;
""")


def downgrade():
    raise RuntimeError(
        "Keep subject provenance; roll back the application instead of deleting evidence links."
    )
