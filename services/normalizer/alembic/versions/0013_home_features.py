"""Persist the daily homepage selection across processes and restarts."""

from alembic import op

revision = "0013_home_features"
down_revision = "0012_subject_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE genchi_private.home_features (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(id),
    selection_date DATE NOT NULL,
    milestone_ids JSONB NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
UPDATE "SchemaContract" SET minor=10,"updatedAt"=NOW() WHERE id=1;
""")


def downgrade():
    raise RuntimeError("Keep the daily selection; roll back the application instead.")
