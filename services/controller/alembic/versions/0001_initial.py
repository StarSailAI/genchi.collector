"""Create the generic AllFeeds control and resource schema."""

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


DDL = r"""
CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
    fetcher TEXT NOT NULL,
    sink TEXT NOT NULL DEFAULT 'postgres',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    spec JSONB NOT NULL,
    config_hash TEXT NOT NULL,
    managed_by TEXT NOT NULL DEFAULT 'config',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE schedules (
    schedule_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    schedule_spec JSONB NOT NULL,
    next_run_at TIMESTAMPTZ,
    active_task_id BIGINT,
    last_registered_at TIMESTAMPTZ,
    last_finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX schedules_due_idx ON schedules (next_run_at)
    WHERE enabled AND next_run_at IS NOT NULL;

CREATE TABLE task_batches (
    id BIGSERIAL PRIMARY KEY,
    batch_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    total_tasks INT NOT NULL DEFAULT 0,
    completed_tasks INT NOT NULL DEFAULT 0,
    failed_tasks INT NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT 'control-api',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CHECK (status IN ('running','paused','completed','partial','cancelled'))
);

CREATE TABLE tasks (
    id BIGSERIAL PRIMARY KEY,
    dedupe_key TEXT UNIQUE NOT NULL,
    schedule_id TEXT REFERENCES schedules(schedule_id) ON DELETE SET NULL,
    batch_id BIGINT REFERENCES task_batches(id) ON DELETE SET NULL,
    parent_task_id BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
    operation TEXT NOT NULL DEFAULT 'fetch',
    workload TEXT NOT NULL DEFAULT 'manual',
    source_id TEXT NOT NULL,
    source_snapshot JSONB NOT NULL,
    priority INT NOT NULL DEFAULT 20,
    status TEXT NOT NULL DEFAULT 'pending',
    scheduled_for TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    queue TEXT NOT NULL DEFAULT 'default',
    required_capabilities TEXT[] NOT NULL DEFAULT '{}',
    resource_requirements JSONB NOT NULL DEFAULT '{}'::jsonb,
    slot_cost INT NOT NULL DEFAULT 1,
    preferred_worker TEXT,
    execution_timeout_seconds INT NOT NULL DEFAULT 300,
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    locked_by TEXT,
    locked_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    lease_token UUID,
    last_error_class TEXT,
    last_error TEXT,
    created_by TEXT NOT NULL DEFAULT 'scheduler',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (operation IN ('fetch','backfill','maintenance')),
    CHECK (workload IN ('scheduled','manual','backfill','maintenance')),
    CHECK (status IN ('pending','retry','running')),
    CHECK (slot_cost >= 1)
);
CREATE INDEX tasks_claim_idx ON tasks (priority, not_before, id)
    WHERE status IN ('pending','retry');
CREATE INDEX tasks_running_idx ON tasks (locked_by, heartbeat_at)
    WHERE status='running';
CREATE INDEX tasks_batch_idx ON tasks (batch_id) WHERE batch_id IS NOT NULL;

CREATE TABLE task_runs (
    id BIGSERIAL PRIMARY KEY,
    task_id BIGINT NOT NULL,
    dedupe_key TEXT NOT NULL,
    schedule_id TEXT,
    batch_id BIGINT,
    operation TEXT NOT NULL,
    workload TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_snapshot JSONB NOT NULL,
    final_status TEXT NOT NULL,
    attempts INT NOT NULL,
    worker_id TEXT,
    lease_token UUID,
    rows_added INT NOT NULL DEFAULT 0,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_class TEXT,
    error_message TEXT,
    scheduled_for TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    duration_seconds DOUBLE PRECISION,
    UNIQUE (dedupe_key)
);
CREATE INDEX task_runs_finished_idx ON task_runs (finished_at DESC);
CREATE INDEX task_runs_source_idx ON task_runs (source_id, finished_at DESC);
CREATE INDEX task_runs_status_idx ON task_runs (final_status, finished_at DESC);

CREATE TABLE workers (
    node_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    hostname TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'resident',
    desired_state TEXT NOT NULL DEFAULT 'online',
    effective_state TEXT NOT NULL DEFAULT 'online',
    max_concurrency INT NOT NULL,
    capabilities TEXT[] NOT NULL DEFAULT '{}',
    queues TEXT[] NOT NULL DEFAULT '{default}',
    plugins JSONB NOT NULL DEFAULT '[]'::jsonb,
    software_version TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (mode IN ('resident','burst')),
    CHECK (desired_state IN ('online','draining','disabled'))
);
CREATE INDEX workers_heartbeat_idx ON workers (last_heartbeat_at DESC);

CREATE TABLE worker_enrollments (
    id BIGSERIAL PRIMARY KEY,
    token_hash CHAR(64) UNIQUE NOT NULL,
    mode TEXT NOT NULL,
    max_uses INT NOT NULL DEFAULT 1,
    uses INT NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE worker_credentials (
    node_id TEXT PRIMARY KEY REFERENCES workers(node_id) ON DELETE CASCADE,
    credential_hash CHAR(64) UNIQUE NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

CREATE TABLE resource_leases (
    resource_key TEXT NOT NULL,
    slot INT NOT NULL,
    task_id BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    lease_token UUID NOT NULL,
    worker_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (resource_key, slot),
    UNIQUE (task_id, resource_key)
);
CREATE INDEX resource_leases_expiry_idx ON resource_leases (expires_at);

CREATE TABLE fetch_states (
    source_id TEXT PRIMARY KEY,
    checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb,
    etag TEXT,
    last_modified TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE resources (
    id BIGSERIAL PRIMARY KEY,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'document',
    url TEXT,
    title TEXT,
    content TEXT,
    content_type TEXT,
    language TEXT,
    published_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content_hash CHAR(64) NOT NULL,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, external_id)
);
CREATE INDEX resources_source_time_idx ON resources (source_id, published_at DESC);
CREATE INDEX resources_observed_idx ON resources (observed_at DESC);
CREATE INDEX resources_kind_idx ON resources (kind);
CREATE INDEX resources_attributes_idx ON resources USING GIN (attributes);

CREATE TABLE resource_versions (
    id BIGSERIAL PRIMARY KEY,
    resource_id BIGINT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    content_hash CHAR(64) NOT NULL,
    title TEXT,
    content TEXT,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (resource_id, content_hash)
);

CREATE TABLE resource_assets (
    id BIGSERIAL PRIMARY KEY,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    asset_key TEXT NOT NULL,
    url TEXT,
    media_type TEXT,
    size_bytes BIGINT,
    checksum TEXT,
    storage_uri TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, external_id, asset_key)
);
CREATE INDEX resource_assets_external_idx ON resource_assets (source_id, external_id);
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS resource_assets;
        DROP TABLE IF EXISTS resource_versions;
        DROP TABLE IF EXISTS resources;
        DROP TABLE IF EXISTS fetch_states;
        DROP TABLE IF EXISTS resource_leases;
        DROP TABLE IF EXISTS worker_credentials;
        DROP TABLE IF EXISTS worker_enrollments;
        DROP TABLE IF EXISTS workers;
        DROP TABLE IF EXISTS task_runs;
        DROP TABLE IF EXISTS tasks;
        DROP TABLE IF EXISTS task_batches;
        DROP TABLE IF EXISTS schedules;
        DROP TABLE IF EXISTS sources;
        """
    )
