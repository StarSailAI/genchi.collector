"""Distinguish artist follows from franchise follows without changing stable slugs."""

from alembic import op

revision = "0014_artist_subjects"
down_revision = "0013_home_features"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
ALTER TABLE catalog_subjects ADD COLUMN subject_type TEXT NOT NULL DEFAULT 'FRANCHISE'
  CHECK(subject_type IN ('FRANCHISE','ARTIST'));
CREATE INDEX catalog_subject_type ON catalog_subjects(subject_type,slug);
UPDATE "SchemaContract" SET minor=11,"updatedAt"=NOW() WHERE id=1;
""")


def downgrade():
    raise RuntimeError("Artist follows and their subject type must be retained; roll back the application.")
