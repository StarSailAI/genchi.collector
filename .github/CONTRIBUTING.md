# Contributing to Genchi Collector

Start with the [project overview](../README.md), [documentation index](../docs/README.md)
and [development setup](../docs/development/getting-started.md). Coding agents must
also read [AGENTS.md](../AGENTS.md).

## Changes and checks

Create a focused branch and explain the problem, resulting behavior and validation
in your pull request. Add tests for behavior changes. Run the lint, tests, Source
validation, document links and release hygiene checks in the development guide.
Database changes require PostgreSQL integration coverage with isolated schemas.

Public SDK and HTTP protocol changes need a compatibility note. Add migrations
rather than editing released migrations. Fetchers need stable external IDs, typed
failure semantics and the shared HTTP safety layer; see [plugin development](../docs/development/plugin-development.md).

Do not include credentials, account cookies, private data or production screenshots.
Keep local collection and notification services stopped; use offline tests locally
and the controlled server workflow for live source acceptance.

## Documentation

The root README is the Chinese product overview; its English counterpart is
[docs/i18n/README.en.md](../docs/i18n/README.en.md). Keep both aligned. Add detailed
material to the appropriate section under `docs/` and link it from the index.
Dated reports belong under `docs/archive/`; package README files stay with their code.
Run `python3 scripts/check-doc-links.py` after moving or editing documents.

Follow the [code of conduct](CODE_OF_CONDUCT.md). Report vulnerabilities privately
using the [security policy](SECURITY.md), not a public issue.
