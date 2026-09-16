# Changelog

## Unreleased

- Reorganize documentation into guides, architecture, development, reference, operations, sources and archives.
- Replace inherited framework landing pages with aligned Chinese and English Genchi overviews.
- Keep root agent instructions concise while preserving the full engineering reference.
- Check local documentation links and heading anchors in CI.

## 2026-09-16 — Public repository

- Publish Genchi Collector to GitHub with upstream history and MIT attribution.
- Add private configuration exclusions, local credential initialization and release hygiene checks.
- Validate the source snapshot, reachable history, tests and all ten distributable packages.

## 0.1.0 - 2026-07-18

- Initial standalone Controller and Worker protocol.
- Python entry-point Fetcher, Sink and Asset Store SDK.
- Generic PostgreSQL Resource, version and asset model.
- RSS, HTML page, HTML list/detail, JSON API and Sitemap Fetchers.
- Windowed backfill, weighted slots and cluster resource leases.
- Read-only operations dashboard and Prometheus metrics.
