# Agent notes for the open-source collector

This repository contains only the self-hosted collection core. Read `README.md`,
`docs/architecture.md`, and `docs/getting-started.md` before changing it.

Preserve Controller/Worker separation, PostgreSQL leases, idempotent writes,
immutable Source snapshots, HTTP safety checks, and fail-closed authentication.
Do not add Genchi production credentials, private source lists, cookies, user data,
or website/account/notification code. Keep example Sources disabled by default.

For code changes run `ruff check .`, `pytest -q`, Source validation, and
`python3 scripts/check-public-release.py --history`. Use an isolated test database
for PostgreSQL integration tests; never point tests at production.
