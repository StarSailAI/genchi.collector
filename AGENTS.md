# Genchi Collector: agent instructions

Start here when modifying this repository. This is the Genchi collection and
product backend, derived from AllFeeds; `allfeeds-*` package names are intentional.
The user-facing Next.js website lives in the separate `genchi.news` repository.

## Read before editing

- Use [the documentation index](docs/README.md) to find the relevant topic.
- Before code changes, read [engineering invariants](docs/development/engineering.md).
  Its detailed rules remain mandatory; this file is the entry point, not a replacement.
- For setup and commands, read [development setup](docs/development/getting-started.md).
- For product changes, read [the product model](docs/architecture/product-v2.md).
- For deployment, read [production operations](docs/operations/production.md).
- Historical files under `docs/archive/` are evidence from a specific date, not
  current deployment instructions or authorization to repeat an operation.

## Repository map

| Path | Responsibility |
| --- | --- |
| `packages/contracts/`, `packages/sdk/` | Shared protocol and plugin interfaces |
| `services/controller/` | Scheduling, leases, credentials and framework migrations |
| `services/worker/` | Task execution, isolation and idempotent sinks |
| `services/browser/` | Private authenticated Camoufox adapter and verification |
| `services/normalizer/` | Curated migrations, raw indexing and normalization |
| `services/product/` | Catalog, accounts, review, APIs and notification outbox |
| `plugins/builtin/`, `plugins/genchi/` | Generic and domain-specific Fetchers |
| `dashboard/` | Read-only operations interface |
| `config/` | Explicit catalog scope and immutable Source definitions |
| `deploy/`, `scripts/` | Operations tools and local/release checks |
| `tests/` | Unit tests and isolated PostgreSQL integration coverage |

## Non-negotiable boundaries

1. The Controller schedules; Workers execute. PostgreSQL is authoritative.
   Preserve leases, bounded wakeups, at-least-once delivery and idempotent writes.
2. Keep raw state in `allfeeds`, curated content in `genchi`, and account state in
   `genchi_private`. The website never writes the database directly.
3. Secrets use environment references. Never put real credentials, account cookies,
   internal hosts, production screenshots or user data in Git, logs or Source snapshots.
4. Keep SSRF protection, redirect checks, payload limits and authentication fail-closed.
   X collection uses the private Camoufox adapter; cookie login requires explicit config.
5. Do not broaden source scope or add X historical backfill implicitly. New sources
   need explicit catalog configuration, stable IDs, tests and per-source acceptance.
6. Uncertain extraction stays in review. Preserve original titles, evidence, source
   IDs, round keys and editor overrides. Every LLM extraction/relevance call includes
   `glossary_prompt()`. DATE/TBD never imply fabricated precise timestamps.
7. Add migrations; never edit released migrations or discard stored data. Update
   `SchemaContract` for curated changes and coordinate incompatible website contracts.
8. Preserve atomic challenge consumption, committed rate-limit failures, signed BFF
   attribution, explicit locales and send-time notification eligibility. Never blindly
   retry uncertain SMTP delivery or send real test email without user authorization.

## Working and validation

Local work is for editing and offline tests. Keep local collection Workers,
normalizer, notifier and collection browser stopped. Live collection and upstream
acceptance run on the deployed server; production source schedules remain daily
and staggered after individual acceptance.

From the repository root, after installing development dependencies:

```bash
ruff check .
pytest -q
allfeeds-plugin validate --sources config/sources.yaml
allfeeds-control config-validate --sources config/sources.yaml
python3 scripts/check-doc-links.py
python3 scripts/check-public-release.py --history
```

Set `ALLFEEDS_TEST_DATABASE_URL` for integration tests; use unique temporary schemas
and drop only test schemas. Add behavior tests for code changes; documentation-only
changes need link and consistency checks, not new application tests. Build all
packages before a release as described in the development guide.

Before deploying a product image, run `deploy/check-product-image.py` inside that
image without network access. Preserve concurrent changes and existing credentials.
Deploy compatible product/notifier code together after migrations and verify health.
Documentation-only changes are published to GitHub and do not restart services.

## Documentation ownership

- Root Markdown files are **README.md** (Chinese product overview) and **AGENTS.md**
  (agent entry point). Keep the MIT `LICENSE` at the root.
- Keep `docs/i18n/README.en.md` aligned with the root README. Do not create another
  Chinese README or restore the inherited AllFeeds marketing page.
- Community files live in `.github/`: contribution guide, security policy and code
  of conduct. Project history and attribution live in `docs/project/`.
- Place guides, architecture, reference, development, operations and source documents
  in their respective `docs/` directories. Add them to `docs/README.md`.
- Keep dated acceptance records and reports under `docs/archive/`, clearly labeled
  historical. Do not append old release claims to current setup instructions.
- Package-level README files stay beside their packages. Commands in guides run from
  the repository root unless another working directory is explicitly stated.
- Use relative Markdown links; update inbound links when moving files. Run the link
  checker after editing documentation. Preserve mandatory engineering rules when
  shortening this file; link to the detailed reference rather than dropping them.
