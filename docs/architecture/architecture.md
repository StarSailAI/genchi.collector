# Architecture

## Control Plane

The Controller is the only scheduler. It reconciles declarative Sources into
database schedules and turns due schedules into immutable tasks. Manual and
backfill tasks enter the same task pool.

PostgreSQL is the source of truth for Sources, schedules, tasks, task runs,
workers, enrollments, credentials and resource leases. `SKIP LOCKED` permits
multiple claim requests without duplicate assignment. Every assignment receives
a UUID lease token; a previous owner cannot submit a late result after a lease
has been recovered.

PostgreSQL `NOTIFY` wakes one bounded local waiter at a time. A timeout remains
as a recovery path because notifications are intentionally not durable.

## Workers

Workers have no inbound port. They enroll once, store a long-lived credential,
and use authenticated outbound requests to claim and report tasks. A task runs
in its own spawned child process. The parent enforces the task timeout and keeps
heartbeats independent from fetcher code.

Workers report installed plugin inventory and capabilities. A task requiring
`fetcher:builtin.rss:api-v1` can only be claimed by a Worker reporting that exact
capability.

`resident` Workers protect normal collection by limiting backfill slots.
`burst` Workers can use their full capacity for backlog and backfill.

## Plugin and Data Plane

Fetcher plugins validate Source config and emit standard Resources through an
SDK context. Fetchers do not receive Controller internals or task-pool database
access. Sink plugins handle persistence. The built-in PostgreSQL Sink maintains
the latest Resource and an append-only version whenever the content hash changes.

Files are represented by ResourceAsset. The built-in Worker can write bytes to
a mounted local/shared filesystem and store the resulting URI in PostgreSQL.

Task delivery is at least once. Custom Sinks must implement stable uniqueness or
another idempotency mechanism.
