"""Create the Genchi curated schema and raw-resource outbox."""

from alembic import op

revision = "0001_genchi_curated"
down_revision = None
branch_labels = None
depends_on = None


DDL = r'''
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TYPE "EventType" AS ENUM ('LIVE','FES','ANNIV','RELEASE_EVENT','RADIO','OTHER');
CREATE TYPE "EventStatus" AS ENUM ('SCHEDULED','ONGOING','FINISHED','CANCELED','POSTPONED');
CREATE TYPE "TicketPhase" AS ENUM ('FC_PRE','LOTTERY_1','LOTTERY_2','LOTTERY_3','ADVANCE','GENERAL','DAY_OF','RESALE','OTHER');
CREATE TYPE "TicketPlatform" AS ENUM ('eplus','lawson','pia','cnplayguide','official','other');
CREATE TYPE "TicketStatus" AS ENUM ('UPCOMING','OPEN','CLOSED','RESULT_ANNOUNCED','CANCELED');
CREATE TYPE "ReleaseKind" AS ENUM ('CD','BD','DIGITAL','GOODS');
CREATE TYPE "NewsKind" AS ENUM ('RELEASE','EVENT','MEDIA','OTHER');
CREATE TYPE "SourceKind" AS ENUM ('OFFICIAL','TWITTER','MEDIA','USER','AGGREGATOR');
CREATE TYPE "EventArtistRole" AS ENUM ('MAIN','GUEST','BAND','MC');

CREATE TABLE "SchemaContract" (
    "id" INTEGER PRIMARY KEY DEFAULT 1 CHECK ("id"=1),
    "major" INTEGER NOT NULL,
    "minor" INTEGER NOT NULL,
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO "SchemaContract" ("major","minor") VALUES (1,0);

CREATE TABLE "Ip" (
    "id" TEXT PRIMARY KEY,
    "slug" TEXT UNIQUE NOT NULL,
    "nameJa" TEXT,
    "nameZh" TEXT,
    "nameEn" TEXT,
    "colorHex" TEXT,
    "iconPath" TEXT,
    "sortOrder" INTEGER NOT NULL DEFAULT 0,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE "Franchise" (
    "id" TEXT PRIMARY KEY,
    "slug" TEXT UNIQUE NOT NULL,
    "ipId" TEXT NOT NULL REFERENCES "Ip"("id"),
    "nameJa" TEXT,
    "nameZh" TEXT,
    "sortOrder" INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX "Franchise_ipId_idx" ON "Franchise"("ipId");
CREATE TABLE "Group" (
    "id" TEXT PRIMARY KEY,
    "slug" TEXT UNIQUE NOT NULL,
    "ipId" TEXT NOT NULL REFERENCES "Ip"("id"),
    "franchiseId" TEXT REFERENCES "Franchise"("id") ON DELETE SET NULL,
    "nameJa" TEXT,
    "nameZh" TEXT,
    "shortName" TEXT,
    "colorHex" TEXT,
    "iconPath" TEXT,
    "debutOn" TIMESTAMPTZ,
    "isActive" BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX "Group_ipId_idx" ON "Group"("ipId");
CREATE INDEX "Group_franchiseId_idx" ON "Group"("franchiseId");
CREATE TABLE "Artist" (
    "id" TEXT PRIMARY KEY,
    "slug" TEXT UNIQUE NOT NULL,
    "nameJa" TEXT,
    "nameZh" TEXT,
    "kana" TEXT,
    "birthday" TIMESTAMPTZ,
    "defaultGroupId" TEXT REFERENCES "Group"("id") ON DELETE SET NULL
);
CREATE TABLE "Venue" (
    "id" TEXT PRIMARY KEY,
    "slug" TEXT UNIQUE NOT NULL,
    "nameJa" TEXT,
    "nameZh" TEXT,
    "city" TEXT,
    "prefecture" TEXT,
    "capacity" INTEGER,
    "officialUrl" TEXT,
    "mapUrl" TEXT,
    "lat" DOUBLE PRECISION,
    "lng" DOUBLE PRECISION
);
CREATE TABLE "Source" (
    "id" TEXT PRIMARY KEY,
    "key" TEXT UNIQUE NOT NULL,
    "name" TEXT NOT NULL,
    "url" TEXT,
    "kind" "SourceKind" NOT NULL DEFAULT 'OFFICIAL',
    "projectKey" TEXT,
    "country" TEXT NOT NULL DEFAULT 'JP',
    "timezone" TEXT NOT NULL DEFAULT 'Asia/Tokyo',
    "isActive" BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX "Source_projectKey_idx" ON "Source"("projectKey");
CREATE TABLE "Event" (
    "id" TEXT PRIMARY KEY,
    "slug" TEXT UNIQUE NOT NULL,
    "sourceKey" TEXT UNIQUE,
    "titleJa" TEXT,
    "titleZh" TEXT,
    "subtitle" TEXT,
    "startsAt" TIMESTAMPTZ NOT NULL,
    "endsAt" TIMESTAMPTZ,
    "doorsAt" TIMESTAMPTZ,
    "venueId" TEXT REFERENCES "Venue"("id") ON DELETE SET NULL,
    "ipId" TEXT REFERENCES "Ip"("id") ON DELETE SET NULL,
    "franchiseId" TEXT REFERENCES "Franchise"("id") ON DELETE SET NULL,
    "eventType" "EventType" NOT NULL DEFAULT 'LIVE',
    "status" "EventStatus" NOT NULL DEFAULT 'SCHEDULED',
    "officialUrl" TEXT,
    "keyVisualUrl" TEXT,
    "setlistRaw" TEXT,
    "notes" TEXT,
    "sourceId" TEXT REFERENCES "Source"("id") ON DELETE SET NULL,
    "seriesKey" TEXT,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX "Event_startsAt_idx" ON "Event"("startsAt");
CREATE INDEX "Event_ipId_startsAt_idx" ON "Event"("ipId","startsAt");
CREATE INDEX "Event_venueId_idx" ON "Event"("venueId");
CREATE TABLE "TicketWindow" (
    "id" TEXT PRIMARY KEY,
    "sourceKey" TEXT UNIQUE,
    "eventId" TEXT NOT NULL REFERENCES "Event"("id") ON DELETE CASCADE,
    "phase" "TicketPhase" NOT NULL,
    "phaseLabelJa" TEXT,
    "phaseLabelZh" TEXT,
    "opensAt" TIMESTAMPTZ NOT NULL,
    "closesAt" TIMESTAMPTZ,
    "resultAt" TIMESTAMPTZ,
    "platform" "TicketPlatform" NOT NULL DEFAULT 'official',
    "url" TEXT,
    "priceJpy" INTEGER,
    "notes" TEXT,
    "status" "TicketStatus",
    "sortOrder" INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX "TicketWindow_eventId_opensAt_idx" ON "TicketWindow"("eventId","opensAt");
CREATE TABLE "ReleaseItem" (
    "id" TEXT PRIMARY KEY,
    "slug" TEXT UNIQUE NOT NULL,
    "sourceKey" TEXT UNIQUE,
    "titleJa" TEXT,
    "titleZh" TEXT,
    "kind" "ReleaseKind" NOT NULL DEFAULT 'CD',
    "releaseOn" TIMESTAMPTZ NOT NULL,
    "groupId" TEXT REFERENCES "Group"("id") ON DELETE SET NULL,
    "ipId" TEXT REFERENCES "Ip"("id") ON DELETE SET NULL,
    "officialUrl" TEXT,
    "jacketUrl" TEXT,
    "notes" TEXT
);
CREATE INDEX "ReleaseItem_releaseOn_idx" ON "ReleaseItem"("releaseOn");

CREATE TABLE "ContentItem" (
    "id" TEXT PRIMARY KEY,
    "sourceId" TEXT NOT NULL REFERENCES "Source"("id"),
    "externalId" TEXT NOT NULL,
    "rawResourceId" BIGINT NOT NULL,
    "rawContentHash" CHAR(64) NOT NULL,
    "kind" TEXT NOT NULL,
    "canonicalUrl" TEXT,
    "titleOriginal" TEXT,
    "bodyOriginal" TEXT,
    "language" TEXT,
    "titleZh" TEXT,
    "summaryZh" TEXT,
    "category" TEXT NOT NULL DEFAULT 'OTHER',
    "projectKey" TEXT,
    "country" TEXT NOT NULL DEFAULT 'JP',
    "timezone" TEXT NOT NULL DEFAULT 'Asia/Tokyo',
    "publishedAt" TIMESTAMPTZ,
    "observedAt" TIMESTAMPTZ NOT NULL,
    "media" JSONB NOT NULL DEFAULT '[]'::jsonb,
    "metadata" JSONB NOT NULL DEFAULT '{}'::jsonb,
    "processingStatus" TEXT NOT NULL DEFAULT 'RAW',
    "searchable" BOOLEAN NOT NULL DEFAULT TRUE,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE ("sourceId","externalId")
);
CREATE INDEX "ContentItem_publishedAt_idx" ON "ContentItem"("publishedAt" DESC);
CREATE INDEX "ContentItem_projectKey_idx" ON "ContentItem"("projectKey","publishedAt" DESC);

CREATE TABLE "NewsPost" (
    "id" TEXT PRIMARY KEY,
    "slug" TEXT UNIQUE NOT NULL,
    "contentItemId" TEXT UNIQUE REFERENCES "ContentItem"("id") ON DELETE SET NULL,
    "titleZh" TEXT,
    "titleJa" TEXT,
    "summary" TEXT,
    "contentMd" TEXT,
    "publishedAt" TIMESTAMPTZ NOT NULL,
    "kind" "NewsKind" NOT NULL DEFAULT 'OTHER',
    "sourceId" TEXT REFERENCES "Source"("id") ON DELETE SET NULL,
    "coverUrl" TEXT
);
CREATE INDEX "NewsPost_publishedAt_idx" ON "NewsPost"("publishedAt" DESC);
CREATE TABLE "EventArtist" (
    "eventId" TEXT NOT NULL REFERENCES "Event"("id") ON DELETE CASCADE,
    "artistId" TEXT NOT NULL REFERENCES "Artist"("id"),
    "groupId" TEXT REFERENCES "Group"("id") ON DELETE SET NULL,
    "role" "EventArtistRole" NOT NULL DEFAULT 'MAIN',
    "sortOrder" INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY ("eventId","artistId")
);
CREATE INDEX "EventArtist_artistId_idx" ON "EventArtist"("artistId");
CREATE TABLE "EventNews" (
    "eventId" TEXT NOT NULL REFERENCES "Event"("id") ON DELETE CASCADE,
    "newsId" TEXT NOT NULL REFERENCES "NewsPost"("id") ON DELETE CASCADE,
    PRIMARY KEY ("eventId","newsId")
);
CREATE TABLE "NewsIp" (
    "newsId" TEXT NOT NULL REFERENCES "NewsPost"("id") ON DELETE CASCADE,
    "ipId" TEXT NOT NULL REFERENCES "Ip"("id"),
    PRIMARY KEY ("newsId","ipId")
);
CREATE TABLE "ContentIp" (
    "contentItemId" TEXT NOT NULL REFERENCES "ContentItem"("id") ON DELETE CASCADE,
    "ipId" TEXT NOT NULL REFERENCES "Ip"("id"),
    PRIMARY KEY ("contentItemId","ipId")
);

CREATE TABLE "ExtractionCandidate" (
    "id" TEXT PRIMARY KEY,
    "contentItemId" TEXT NOT NULL REFERENCES "ContentItem"("id") ON DELETE CASCADE,
    "kind" TEXT NOT NULL CHECK ("kind" IN ('EVENT','TICKET','RELEASE')),
    "payload" JSONB NOT NULL,
    "confidence" DOUBLE PRECISION NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'PENDING' CHECK ("status" IN ('PENDING','APPROVED','REJECTED','AUTO_APPLIED')),
    "validationErrors" JSONB NOT NULL DEFAULT '[]'::jsonb,
    "appliedEntityType" TEXT,
    "appliedEntityId" TEXT,
    "reviewedBy" TEXT,
    "reviewedAt" TIMESTAMPTZ,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX "ExtractionCandidate_status_idx" ON "ExtractionCandidate"("status","createdAt");

CREATE TABLE "SearchDocument" (
    "id" TEXT PRIMARY KEY,
    "entityType" TEXT NOT NULL,
    "entityId" TEXT NOT NULL,
    "titleOriginal" TEXT,
    "titleZh" TEXT,
    "bodyOriginal" TEXT,
    "summaryZh" TEXT,
    "projectKey" TEXT,
    "kind" TEXT NOT NULL,
    "country" TEXT NOT NULL DEFAULT 'JP',
    "publishedAt" TIMESTAMPTZ,
    "canonicalUrl" TEXT,
    "searchText" TEXT NOT NULL,
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE ("entityType","entityId")
);
CREATE INDEX "SearchDocument_searchText_trgm_idx" ON "SearchDocument" USING GIN ("searchText" gin_trgm_ops);
CREATE INDEX "SearchDocument_filters_idx" ON "SearchDocument"("country","kind","publishedAt" DESC);

CREATE TABLE "NormalizationJob" (
    "id" BIGSERIAL PRIMARY KEY,
    "resourceId" BIGINT NOT NULL,
    "contentHash" CHAR(64) NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'PENDING' CHECK ("status" IN ('PENDING','PROCESSING','RETRY','DONE','DEAD')),
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "notBefore" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "lockedAt" TIMESTAMPTZ,
    "lastError" TEXT,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "finishedAt" TIMESTAMPTZ,
    UNIQUE ("resourceId","contentHash")
);
CREATE INDEX "NormalizationJob_claim_idx" ON "NormalizationJob"("notBefore","id")
    WHERE "status" IN ('PENDING','RETRY');

INSERT INTO "Ip" ("id","slug","nameJa","nameZh","sortOrder") VALUES
    ('ip-bang-dream','bang-dream','BanG Dream!','BanG Dream!',10),
    ('ip-girls-band-cry','girls-band-cry','ガールズバンドクライ','Girls Band Cry',20),
    ('ip-love-live','love-live','ラブライブ！','Love Live!',30),
    ('ip-idolmaster','idolmaster','アイドルマスター','偶像大师',40)
ON CONFLICT ("slug") DO NOTHING;

CREATE OR REPLACE FUNCTION enqueue_genchi_normalization() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=genchi,allfeeds,public AS $$
BEGIN
    INSERT INTO genchi."NormalizationJob" ("resourceId","contentHash")
    VALUES (NEW.id,NEW.content_hash)
    ON CONFLICT ("resourceId","contentHash") DO NOTHING;
    RETURN NEW;
END;
$$;
CREATE TRIGGER resources_enqueue_genchi
AFTER INSERT OR UPDATE OF content_hash ON allfeeds.resources
FOR EACH ROW EXECUTE FUNCTION enqueue_genchi_normalization();
INSERT INTO "NormalizationJob" ("resourceId","contentHash")
SELECT id,content_hash FROM allfeeds.resources ON CONFLICT DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='genchi_reader') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA genchi TO genchi_reader';
        EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA genchi TO genchi_reader';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA genchi GRANT SELECT ON TABLES TO genchi_reader';
    END IF;
END;
$$;
'''


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS resources_enqueue_genchi ON allfeeds.resources")
    op.execute("DROP FUNCTION IF EXISTS enqueue_genchi_normalization()")
    op.execute("DROP SCHEMA IF EXISTS genchi CASCADE")
