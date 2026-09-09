# Genchi Collector Agent Development Guide

This file is the technical source of truth for coding agents working in this
repository. The public README is intentionally a short product homepage; keep
implementation details here or under `docs/`.

## Genchi Domain Overlay

This repository is an independent, domain-specific derivative of AllFeeds. In
addition to the framework rules below, preserve these boundaries:

1. This repository exclusively owns collection, normalization, database
   migrations and writes. `genchi.news` reads the product API and uses a same-origin
   proxy for authenticated actions; it never writes the database directly.
2. Raw framework state belongs to `allfeeds`; curated website data belongs to
   `genchi`. Do not move raw crawling concerns into the curated schema.
3. X collection must go through the private, authenticated Camoufox adapter.
   API authentication is separate from X login: use anonymous public contexts by
   default; account cookies require explicit configuration. Do not expose
   arbitrary JavaScript execution or a public browser port.
4. Upstream URLs and page contents remain untrusted. Keep SSRF checks, bounded
   payloads, stable external IDs and idempotent writes.
5. Every curated schema change updates `SchemaContract`; incompatible changes
   require a major-version coordination with `genchi.news`.
6. Deterministic validation gates structured Event, Ticket and Release writes.
   Uncertain extraction remains a review candidate, while raw content stays
   searchable.
7. New projects are enabled manually from `config/catalog.yaml`. Do not silently
   broaden crawl scope or add X historical backfill.

## Product Contract

AllFeeds is a generic distributed data collection framework. It separates task
registration from execution and lets one Controller coordinate any number of
Workers. Source-specific behavior belongs in plugins.

Preserve these properties:

1. The Controller schedules and coordinates; it does not run Fetchers.
2. Workers execute compatible tasks; they do not create schedules.
3. PostgreSQL is the single source of truth for task, lease and execution state.
4. Task delivery is at least once. Sinks must be idempotent.
5. Source definitions are immutable snapshots once a task is registered.
6. The Dashboard is read-only. Administrative actions belong in the API or CLI.
7. Secrets are referenced by environment-variable name, never stored in Source
   snapshots, task payloads, logs or dashboard responses.
8. Installed plugins are trusted code, but fetched URLs and upstream content are
   untrusted input.

## Repository Map

```text
packages/contracts/       Shared protocol and Pydantic data models
packages/sdk/             Public Fetcher, Sink and Asset Store interfaces
services/controller/      FastAPI control plane, task pool and migrations
services/worker/          Worker runtime, process isolation and built-in Sink
services/browser/         Restricted Camoufox HTTP adapter
services/normalizer/      Legacy pipeline and curated migrations
services/product/         Canonical catalog, review API, accounts and notification outbox
plugins/builtin/          Generic RSS, web, JSON API and Sitemap Fetchers
plugins/genchi/           Genchi official-site and X Fetchers
dashboard/                Read-only Streamlit operations UI
examples/custom-fetcher/  Minimal third-party plugin package
config/                   Example Source definitions
docs/                     Architecture, deployment, API and security docs
tests/                    Unit and PostgreSQL integration tests
```

## Core Data Contracts

Contracts live in `packages/contracts/src/allfeeds_contracts/models.py`.

- `SourceSpec` describes a Source, its schedule, retry policy, routing and plugin
  configuration.
- `TaskEnvelope` is the immutable unit sent from Controller to Worker.
- `ResourceRecord` is the normalized current representation of collected data.
- `ResourceAsset` describes a downloaded or externally hosted file.
- `FetchReport` is the structured execution summary shown in history.
- `PluginInventory` and `WorkerDescriptor` drive capability-based routing.

Changes to these models affect the network protocol. Keep additions backward
compatible where possible, increment `PROTOCOL_VERSION` for incompatible Worker
API changes, and add contract tests.

## Task Lifecycle

Hot tasks live only in `tasks` with status `pending`, `retry` or `running`.
Terminal tasks are atomically copied to `task_runs` and removed from `tasks`.

```text
pending -> running -> succeeded/partial -> task_runs
                  -> retry -> running
                  -> dead -> task_runs
```

Important rules:

- Lower numeric priority runs first.
- Claims use `FOR UPDATE SKIP LOCKED`.
- Every claim receives a UUID lease token.
- Completion and failure reports must match the current lease token.
- Worker heartbeats renew task and named-resource leases.
- Maintenance recovers stale tasks and marks missing Workers offline.
- A delayed retry uses the Source retry policy or typed `Retry-After` value.
- Backfill is split into time windows in a `task_batches` row.
- Paused batches remain in the pool but cannot be claimed.
- Named resources limit cluster-wide concurrency for APIs, domains or tokens.

Do not reintroduce final states into the hot `tasks` table.

## Controller

The Controller implementation is under
`services/controller/src/allfeeds_control/`.

- `api.py`: FastAPI endpoints and authentication boundaries.
- `store.py`: transactional task, Worker, batch and overview operations.
- `registrar.py`: Source reconciliation, due schedule registration and
  maintenance loop.
- `signals.py`: PostgreSQL `LISTEN/NOTIFY` bridge using bounded wake tokens.
- `db.py`: psycopg connection pool and schema validation.
- `scheduling.py`: interval, cron and one-shot calculations.
- `metrics.py`: Prometheus text exposition.
- `cli.py`: server, migration and operational commands.

Controller API rules:

- Admin routes require `X-API-Key`.
- Worker routes require a revocable Bearer credential.
- Enrollment tokens are one-time or bounded-use values stored only as hashes.
- Missing `CONTROL_API_TOKEN` is fail-closed unless the explicit local-only
  override is enabled.
- Long-poll claims must remain asynchronous and must not occupy one DB connection
  while waiting.
- Do not broadcast one task notification to every waiting Worker. Preserve the
  bounded-token signal behavior.

## Worker

The Worker implementation is under `services/worker/src/allfeeds_worker/`.

- Workers require no inbound port.
- Enrollment exchanges a short-lived token for a persistent credential.
- A Worker advertises installed plugins, queues, mode and weighted slot capacity.
- Every task runs in a spawned child process with a hard timeout.
- `slot_cost` controls local concurrency; it is not task progress.
- Graceful shutdown enters draining behavior and waits for children.
- A replaced or disabled Worker must stop claiming new work.

Keep child-process inputs serializable. Never pass live DB connections, HTTP
sessions or locks through the process boundary.

## Plugin System

Public interfaces live in `packages/sdk/src/allfeeds_sdk/`. Plugins are found
through standard Python entry points:

```toml
[project.entry-points."allfeeds.fetchers"]
example = "example_package:ExampleFetcher"

[project.entry-points."allfeeds.sinks"]
example = "example_package:ExampleSink"

[project.entry-points."allfeeds.asset_stores"]
example = "example_package:ExampleAssetStore"
```

A Fetcher must provide:

- a stable `FetcherManifest.name`;
- a Pydantic `config_model` with `extra="forbid"`;
- stable upstream `external_id` values;
- `fetch()` and, when appropriate, `backfill()`;
- typed SDK exceptions for configuration, authentication, rate limit,
  transient and permanent failures;
- useful `FetchReport` counts, bytes and content-character totals.

Use `FetchContext.emit()` for Resources, `emit_asset()` for files,
`checkpoint()` for previously committed state and `secret()` for environment
secrets. Save checkpoints only after successful writes.

When adding a Fetcher:

1. Implement it in a separate plugin package or `plugins/builtin` when genuinely
   generic.
2. Register its entry point.
3. Add configuration validation tests.
4. Test stable IDs, empty responses, pagination, retry behavior and backfill.
5. Add a disabled example Source.
6. Run `allfeeds-plugin list` and `allfeeds-plugin validate`.

## Storage Model

The built-in PostgreSQL Sink uses:

- `resources`: current normalized state, unique on `(source_id, external_id)`;
- `resource_versions`: immutable revisions keyed by content hash;
- `resource_assets`: files and external asset metadata;
- `fetch_states`: ETag, Last-Modified and plugin checkpoints.

The Sink may be called more than once for the same task. Duplicate writes must
not create duplicate Resources or versions. A checkpoint must not advance when
the task fails after a partial write.

## Database Migrations

Alembic files live under `services/controller/alembic/`. All framework tables
are created in `ALLFEEDS_DB_SCHEMA`; never assume `public`.

- Add a new numbered migration for every schema change.
- Do not edit an already released migration.
- Validate schema identifiers before interpolating them.
- Make migrations safe for an existing populated installation.
- Add or extend the PostgreSQL integration test.
- Do not silently delete Resource or task history during an upgrade.

## HTTP and Security

The built-in HTTP client is in `plugins/builtin/src/allfeeds_builtin/http.py`.
Preserve these protections:

- only explicitly supported schemes;
- DNS resolution and private, loopback, link-local, multicast and reserved-IP
  blocking;
- redirect target revalidation;
- robots.txt support;
- bounded response size, retry count, redirects and per-host rate;
- conditional requests with ETag and Last-Modified;
- `Retry-After` support for rate limits.

An opt-in private-network mode is acceptable for trusted deployments, but it
must never be the default.

## Dashboard

The Dashboard consumes the Controller API rather than connecting directly to
PostgreSQL. Keep it read-only and operationally focused:

- current task counts;
- online, draining and offline Workers;
- weighted slot usage;
- running and pending queues;
- paginated errors and recent runs;
- schedules and Resource freshness;
- healthy, attention and error state.

Use the shared dark theme. Avoid automatic refresh intervals shorter than five
seconds and avoid expensive per-row API calls.

## Development Workflow

Live crawling, browser probes and upstream/API acceptance run on the deployed
server only. Keep local worker, normalizer, notifier and collection browser
stopped; local work is limited to editing and offline tests. Production sources
run once daily, with staggered schedules, after source-by-source acceptance.

Python 3.11 or newer is required. From the repository root:

```bash
python -m pip install -e packages/contracts -e packages/sdk \
  -e services/controller -e services/worker -e plugins/builtin \
  -e plugins/genchi -e services/normalizer -e services/product -e dashboard \
  -e examples/custom-fetcher -e '.[dev]'

ruff check .
pytest -q
allfeeds-plugin validate --sources config/sources.yaml
allfeeds-control config-validate --sources config/sources.yaml
```

Set `ALLFEEDS_TEST_DATABASE_URL` to run PostgreSQL integration coverage. Tests
must create a unique temporary schema and drop only that schema in cleanup.

Build every distributable package before a release:

```bash
python -m build packages/contracts
python -m build packages/sdk
python -m build services/controller
python -m build services/worker
python -m build plugins/builtin
python -m build dashboard
```

## Documentation Rules

- Keep the root README short, visual and product-oriented.
- Keep both `readme/README_EN.md` and `readme/README_ZH.md` in sync.
- Put architecture and operations detail under `docs/`.
- Put instructions intended primarily for coding agents in this file.
- Never add real credentials, internal hostnames or production screenshots.
- Enabled Sources must be official public endpoints, carry project/country/timezone
  tags and have a tested stable external-ID strategy.

## Definition of Done

A change is complete only when:

1. Public contracts and compatibility impact were considered.
2. Task lease and idempotency guarantees remain intact.
3. Security boundaries remain fail-closed.
4. Tests cover the changed behavior, including PostgreSQL when transactional
   behavior changed.
5. `ruff check .` and `pytest -q` pass.
6. Plugin and Source configuration validation pass when relevant.
7. User-facing docs are updated without turning the README into a developer
   manual.

## Product v2 invariants

- Read `docs/product-v2.md` for the current domain and deployment model.
- Candidate model output never updates published activities before review.
- Stable upstream mappings survive corrections and explicit official grouping.
- DATE and TBD must never generate precise reminders or fabricated timestamps.
- Account tables stay in `genchi_private`; never grant them to the website reader.
- Recheck membership, participation, cancellation and revision immediately before SMTP.
- An uncertain SMTP result needs operator investigation; never blindly retry it.
- Local acceptance uses Mailpit. Do not send real external email without explicit authorization.

## Naming invariants

- `services/normalizer/src/genchi_normalizer/data/glossary.json` is the single editorial glossary. Every LLM extraction / relevance call must include `glossary_prompt()`.
- Preserve original titles, source identifiers and round keys. Normalize only display fields; never merge by translated title.
- Naming edits use `catalog_names` / `catalog_name_history`, not event revision or notification changes. Preserve an editor override until its original source name changes.
- Read `docs/naming.md` before changing glossary or title handling. Coordinate schema 1.3 with the website.
