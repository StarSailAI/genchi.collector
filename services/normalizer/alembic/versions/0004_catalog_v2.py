"""Canonical activity catalog and isolated product accounts / notification outbox."""

from alembic import op

revision = "0004_catalog_v2"
down_revision = "0003_activity_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE catalog_subjects (
 slug TEXT PRIMARY KEY, name TEXT NOT NULL, name_zh TEXT, kind TEXT NOT NULL DEFAULT 'WORK',
 parent_slug TEXT REFERENCES catalog_subjects(slug), aliases JSONB NOT NULL DEFAULT '[]',
 description TEXT, color TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO catalog_subjects(slug,name,name_zh,color)
 SELECT slug,COALESCE("nameJa","nameZh",slug),"nameZh","colorHex" FROM "Ip"
 WHERE slug NOT IN ('unknown','anime-general');
INSERT INTO catalog_subjects(slug,name,name_zh,parent_slug,aliases,description,color) VALUES
 ('gakumas','学園アイドルマスター','学园偶像大师','idolmaster',
 '["学マス","学園アイドルマスター","学园偶像大师","学院偶像大师","gakumas"]',
 '关注学マ斯的演出、快闪、联动咖啡与相关预约、物贩。','#b7543c');

CREATE TABLE catalog_activities (
 id TEXT PRIMARY KEY, identity_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, title_zh TEXT,
 kind TEXT NOT NULL CHECK(kind IN ('LIVE','FESTIVAL','POPUP','CAFE','EXHIBITION','MEETUP','GOODS','OTHER')),
 attendance TEXT NOT NULL DEFAULT 'OFFLINE' CHECK(attendance IN ('OFFLINE','ONLINE','HYBRID','UNKNOWN')),
 status TEXT NOT NULL DEFAULT 'ANNOUNCED' CHECK(status IN ('ANNOUNCED','SCHEDULED','POSTPONED','CANCELED','ENDED')),
 publication TEXT NOT NULL DEFAULT 'REVIEW' CHECK(publication IN ('PUBLISHED','REVIEW','REJECTED')),
 summary TEXT, official_url TEXT, image_url TEXT, revision INTEGER NOT NULL DEFAULT 1,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX catalog_activity_public ON catalog_activities(publication,updated_at DESC);
CREATE TABLE catalog_activity_subjects (
 activity_id TEXT NOT NULL REFERENCES catalog_activities(id), subject_slug TEXT NOT NULL REFERENCES catalog_subjects(slug),
 PRIMARY KEY(activity_id,subject_slug)
);
CREATE TABLE catalog_occurrences (
 id TEXT PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES catalog_activities(id), identity_key TEXT NOT NULL,
 label TEXT, venue TEXT, city TEXT, starts_at TIMESTAMPTZ, ends_at TIMESTAMPTZ,
 starts_on DATE, ends_on DATE, precision TEXT NOT NULL CHECK(precision IN ('TIME','DATE','TBD')),
 timezone TEXT NOT NULL DEFAULT 'Asia/Tokyo', status TEXT NOT NULL DEFAULT 'SCHEDULED',
 UNIQUE(activity_id,identity_key),
 CHECK(ends_at IS NULL OR starts_at IS NULL OR ends_at>=starts_at),
 CHECK(ends_on IS NULL OR starts_on IS NULL OR ends_on>=starts_on),
 CHECK((precision='TIME' AND starts_at IS NOT NULL) OR (precision='DATE' AND starts_on IS NOT NULL) OR precision='TBD')
);
CREATE INDEX catalog_occurrence_time ON catalog_occurrences(starts_at,starts_on);
CREATE TABLE catalog_milestones (
 id TEXT PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES catalog_activities(id), identity_key TEXT NOT NULL,
 kind TEXT NOT NULL, title TEXT NOT NULL, starts_at TIMESTAMPTZ, ends_at TIMESTAMPTZ,
 starts_on DATE, ends_on DATE, precision TEXT NOT NULL CHECK(precision IN ('TIME','DATE','TBD')),
 timezone TEXT NOT NULL DEFAULT 'Asia/Tokyo', status TEXT NOT NULL DEFAULT 'CONFIRMED'
 CHECK(status IN ('CONFIRMED','REVIEW','UNANNOUNCED','CANCELED','SUPERSEDED')),
 url TEXT, platform TEXT, round_key TEXT, eligibility TEXT, notes TEXT,
 requires TEXT NOT NULL DEFAULT 'NONE' CHECK(requires IN ('NONE','APPLIED','WON')),
 details JSONB NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 UNIQUE(activity_id,identity_key),
 CHECK(ends_at IS NULL OR starts_at IS NULL OR ends_at>=starts_at),
 CHECK(ends_on IS NULL OR starts_on IS NULL OR ends_on>=starts_on),
 CHECK((precision='TIME' AND starts_at IS NOT NULL) OR (precision='DATE' AND starts_on IS NOT NULL) OR precision='TBD')
);
CREATE INDEX catalog_milestone_due ON catalog_milestones(status,starts_at,ends_at);
CREATE TABLE catalog_milestone_scopes (
 milestone_id TEXT NOT NULL REFERENCES catalog_milestones(id), occurrence_id TEXT NOT NULL REFERENCES catalog_occurrences(id),
 PRIMARY KEY(milestone_id,occurrence_id)
);
CREATE TABLE catalog_external_ids (
 key TEXT PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES catalog_activities(id),
 occurrence_id TEXT REFERENCES catalog_occurrences(id), milestone_id TEXT REFERENCES catalog_milestones(id)
);
CREATE TABLE catalog_evidence (
 id TEXT PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES catalog_activities(id),
 milestone_id TEXT REFERENCES catalog_milestones(id), source_id TEXT, external_id TEXT,
 version_hash TEXT, url TEXT, excerpt TEXT NOT NULL, field_path TEXT NOT NULL,
 method TEXT NOT NULL DEFAULT 'legacy-import', verified BOOLEAN NOT NULL DEFAULT FALSE,
 published_at TIMESTAMPTZ, observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX catalog_evidence_activity ON catalog_evidence(activity_id,milestone_id);
CREATE TABLE catalog_relations (
 activity_id TEXT NOT NULL REFERENCES catalog_activities(id), related_id TEXT NOT NULL REFERENCES catalog_activities(id),
 kind TEXT NOT NULL, evidence_id TEXT REFERENCES catalog_evidence(id), PRIMARY KEY(activity_id,related_id,kind),
 CHECK(activity_id<>related_id)
);
CREATE TABLE catalog_changes (
 id BIGSERIAL PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES catalog_activities(id),
 milestone_id TEXT REFERENCES catalog_milestones(id), kind TEXT NOT NULL, summary TEXT NOT NULL,
 before_value JSONB, after_value JSONB, notify BOOLEAN NOT NULL DEFAULT FALSE,
 planned_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE catalog_reviews (
 id TEXT PRIMARY KEY, activity_id TEXT REFERENCES catalog_activities(id), resource_id BIGINT,
 kind TEXT NOT NULL, reason TEXT NOT NULL, payload JSONB NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED')),
 reviewed_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE catalog_jobs (
 resource_id BIGINT PRIMARY KEY, content_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
 attempts INT NOT NULL DEFAULT 0, not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 lease_token TEXT, locked_at TIMESTAMPTZ, last_error TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE OR REPLACE FUNCTION enqueue_catalog_job() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO genchi.catalog_jobs(resource_id,content_hash) VALUES(NEW.id,NEW.content_hash)
 ON CONFLICT(resource_id) DO UPDATE SET content_hash=EXCLUDED.content_hash,status='PENDING',attempts=0,
 not_before=NOW(),lease_token=NULL,locked_at=NULL,updated_at=NOW();
 RETURN NEW;
END; $$;
CREATE TRIGGER resources_enqueue_catalog AFTER INSERT OR UPDATE OF content_hash ON allfeeds.resources
 FOR EACH ROW EXECUTE FUNCTION enqueue_catalog_job();

CREATE SCHEMA IF NOT EXISTS genchi_private;
REVOKE ALL ON SCHEMA genchi_private FROM PUBLIC;
CREATE TABLE genchi_private.accounts (
 id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
 verified_at TIMESTAMPTZ, unsubscribed BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE genchi_private.login_tokens (
 token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES genchi_private.accounts(id),
 expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE genchi_private.sessions (
 token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES genchi_private.accounts(id), expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE genchi_private.follows (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES genchi_private.accounts(id),
 target_type TEXT NOT NULL CHECK(target_type IN ('SUBJECT','ACTIVITY')), target_id TEXT NOT NULL,
 reminder_hours INT NOT NULL DEFAULT 24 CHECK(reminder_hours IN (0,2,24,48)),
 include_children BOOLEAN NOT NULL DEFAULT TRUE, kinds JSONB NOT NULL DEFAULT '[]',
 cities JSONB NOT NULL DEFAULT '[]', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 UNIQUE(account_id,target_type,target_id)
);
CREATE TABLE genchi_private.participation (
 account_id TEXT NOT NULL REFERENCES genchi_private.accounts(id), activity_id TEXT NOT NULL REFERENCES genchi.catalog_activities(id),
 round_key TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('INTERESTED','APPLIED','WON','PURCHASED')),
 PRIMARY KEY(account_id,activity_id,round_key)
);
CREATE TABLE genchi_private.mail_queue (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES genchi_private.accounts(id),
 activity_id TEXT REFERENCES genchi.catalog_activities(id), milestone_id TEXT REFERENCES genchi.catalog_milestones(id),
 milestone_revision INT, kind TEXT NOT NULL, dedup_key TEXT NOT NULL UNIQUE, due_at TIMESTAMPTZ NOT NULL,
 payload JSONB NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'PENDING', attempts INT NOT NULL DEFAULT 0,
 lease_token TEXT, locked_at TIMESTAMPTZ, sent_at TIMESTAMPTZ, last_error TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX mail_queue_pending ON genchi_private.mail_queue(status,due_at);
CREATE TABLE genchi_private.auth_limits (
 key TEXT PRIMARY KEY, window_start TIMESTAMPTZ NOT NULL DEFAULT NOW(), attempts INT NOT NULL DEFAULT 1
);
UPDATE "SchemaContract" SET minor=2,"updatedAt"=NOW() WHERE id=1;
""")


def downgrade() -> None:
    raise RuntimeError(
        "Catalog migrations preserve user data; roll back applications, not this schema."
    )
