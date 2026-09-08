"""Scope upstream milestone identifiers to their containing activity."""

from alembic import op

revision = "0005_scoped_source_keys"
down_revision = "0004_catalog_v2"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "UPDATE catalog_external_ids SET key=activity_id || ':' || key WHERE milestone_id IS NOT NULL AND key NOT LIKE activity_id || ':%'"
    )
    op.execute("""DELETE FROM catalog_milestone_scopes s USING catalog_milestones m,catalog_occurrences o
        WHERE s.milestone_id=m.id AND s.occurrence_id=o.id AND m.activity_id<>o.activity_id""")


def downgrade():
    pass
