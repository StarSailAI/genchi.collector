"""Add reusable activity profiles for calendar discovery and LLM caching."""

from alembic import op

revision = "0003_activity_profiles"
down_revision = "0002_anime_catalog_ips"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE "ActivityProfile" (
            "id" TEXT PRIMARY KEY,
            "slug" TEXT NOT NULL UNIQUE,
            "fingerprint" CHAR(64) NOT NULL UNIQUE,
            "canonicalTitle" TEXT NOT NULL,
            "shortTitle" TEXT,
            "subjectName" TEXT,
            "subjectType" TEXT NOT NULL DEFAULT 'UNKNOWN',
            "ipId" TEXT REFERENCES "Ip"("id"),
            "eventType" "EventType",
            "relevanceConfidence" DOUBLE PRECISION NOT NULL DEFAULT 0,
            "reviewStatus" TEXT NOT NULL DEFAULT 'REVIEW'
                CHECK ("reviewStatus" IN ('ACCEPTED','REVIEW','REJECTED')),
            "facts" JSONB NOT NULL DEFAULT '{}'::jsonb,
            "evidence" JSONB NOT NULL DEFAULT '[]'::jsonb,
            "contentHash" CHAR(64),
            "llmModel" TEXT,
            "promptVersion" TEXT NOT NULL DEFAULT 'activity-v1',
            "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX "ActivityProfile_status_idx"
            ON "ActivityProfile" ("reviewStatus","relevanceConfidence");
        CREATE INDEX "ActivityProfile_ip_idx" ON "ActivityProfile" ("ipId");

        ALTER TABLE "Event"
            ADD COLUMN "activityId" TEXT REFERENCES "ActivityProfile"("id") ON DELETE SET NULL;
        CREATE INDEX "Event_activity_starts_idx" ON "Event" ("activityId","startsAt");

        UPDATE "SchemaContract" SET "minor"=1,"updatedAt"=NOW() WHERE "id"=1;

        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='genchi_reader') THEN
                EXECUTE 'GRANT SELECT ON TABLE "ActivityProfile" TO genchi_reader';
            END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE "Event" DROP COLUMN IF EXISTS "activityId";
        DROP TABLE IF EXISTS "ActivityProfile";
        UPDATE "SchemaContract" SET "minor"=0,"updatedAt"=NOW() WHERE "id"=1;
        """
    )
