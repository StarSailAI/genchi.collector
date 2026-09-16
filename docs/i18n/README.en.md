<p align="center">
  <a href="https://genchi.news">
    <img src="../../assets/genchi-mascot.webp" width="192" height="192" alt="GENCHI mascot" />
  </a>
</p>

<h1 align="center">Genchi Collector</h1>

**Turn Japanese anime, music and live-event announcements into traceable activity timelines.**

[简体中文](../../README.md) · English · [Documentation](../README.md) · [Contributing](../../.github/CONTRIBUTING.md)

Genchi Collector is the collection, activity catalog and notification backend for [genchi.news](https://genchi.news), built on the AllFeeds distributed collection framework. It retains source evidence and turns official websites, public X posts, ticket platforms and reviewed aggregators into activities, occurrences and milestones for websites and agents.

```text
Official sites / X / Ticket platforms / Aggregators
                         ↓
             Schedule → Collect → Raw versions
                         ↓
       Structured validation / Model candidates / Review
                         ↓
       Activities → Occurrences → Ticket and event milestones
                         ↓
             Product API / Agent API / Email reminders
```

## What it does

- **Preserves evidence and versions** with idempotent writes and traceable updates.
- **Models events and ticketing** with separate occurrences, application rounds and explicit time precision; uncertain content remains a review candidate.
- **Distributes collection** across a Controller and Workers, with plugins, retries, leases and backfill.
- **Provides product features** including email-code login, follows, agendas, reminders and catalog-grounded Q&A.
- **Exposes APIs** for the website and REST/MCP clients.

This repository contains the backend, collection plugins and read-only operations Dashboard. The user-facing Next.js website is a separate `genchi.news` project. Enable projects explicitly in the [catalog](../../config/catalog.yaml); review collection scope and schedules in the [source configuration](../../config/sources.yaml).

## Start here

| Goal | Documentation |
| --- | --- |
| Read the code or develop offline | [Development setup and checks](../development/getting-started.md) |
| Deploy an instance | [Single-host deployment](../operations/production.md) |
| Configure secrets or prepare a release | [Open-source configuration](../operations/open-source.md) |
| Understand the system | [Product model](../architecture/product-v2.md) · [Collection architecture](../architecture/architecture.md) |
| Add a source | [Plugin development](../development/plugin-development.md) · [Source documentation](../README.md#数据源) |
| Use the APIs | [Controller API](../reference/api.md) · [Agent API](../reference/agent-api.md) |

Run `python3 scripts/init-local-env.py` from the repository root to create local `.env` configuration. It does not start services. Production uses the separate shared-configuration workflow documented in the deployment guide. Detailed product and operations guides are currently primarily in Chinese.

## Repository layout

```text
packages/       Shared contracts and plugin SDK
services/       Controller, Worker, browser, normalizer and product services
plugins/        Generic and Genchi-specific Fetchers
config/         Catalog and source definitions
dashboard/      Read-only operations interface
deploy/         Deployment and maintenance tools
scripts/        Local initialization and release checks
tests/          Unit and PostgreSQL integration tests
docs/           Topic-based documentation and historical records
```

## Contribute

Read the [contribution guide](../../.github/CONTRIBUTING.md); coding agents should start with [AGENTS.md](../../AGENTS.md). Report vulnerabilities privately using the [security policy](../../.github/SECURITY.md).

[MIT license](../../LICENSE) · [AllFeeds attribution](../project/upstream.md) · [Changelog](../project/changelog.md) · [Code of conduct](../../.github/CODE_OF_CONDUCT.md)
