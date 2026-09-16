# Configuration

Persistent Sources live in `config/sources.yaml`. The Controller validates and
reconciles the file periodically. Removing a Source disables its schedule; it
does not delete collected Resources.

## Source Fields

- `id`: stable unique identifier.
- `fetcher`: Python plugin manifest name.
- `sink`: Sink plugin name, normally `postgres`.
- `schedule`: `interval`, `cron`, or `once`, with an explicit timezone.
- `config`: Fetcher-specific validated configuration.
- `priority`: lower numbers run first.
- `timeout_seconds`: hard child-process timeout.
- `retry`: maximum attempts and fixed/exponential delay.
- `routing.queue`: Worker queue.
- `routing.slot_cost`: local worker capacity used by this task.
- `routing.capabilities`: extra worker capabilities.
- `routing.resources`: named cluster resource and its concurrent capacity.
- `backfill_enabled`: whether the API may create backfill tasks.
- `backfill_window_seconds`: default backfill partition size.

Set `asset_store: local` or `asset_store: s3` when a Fetcher emits downloaded
bytes. The S3-compatible backend reads `S3_BUCKET`, optional `S3_ENDPOINT_URL`,
`S3_REGION` and `S3_PREFIX` from the Worker environment.

Tasks contain an immutable Source snapshot. Updating a Source affects future
scheduled tasks but does not change already registered manual or backfill tasks.

## Secrets

Fetcher configuration stores secret names, not values. For example:

```yaml
config:
  url: https://api.example.com/items
  bearer_token_secret: EXAMPLE_API_TOKEN
```

Set `EXAMPLE_API_TOKEN` in the Worker environment. The value is not placed in
the Source snapshot, task history, API response or dashboard.

## Built-in Fetchers

- `builtin.rss`: `url`, optional `fulltext`, selectors and enclosures.
- `builtin.web_page`: one URL with CSS selectors.
- `builtin.web_list`: paginated list discovery plus detail-page selectors.
- `builtin.json_api`: JMESPath item and field mappings, page/cursor pagination.
- `builtin.sitemap`: Sitemap discovery followed by page extraction.

HTTP defaults honor robots.txt, block private network targets, limit responses
to 10 MB, retry transient errors and rate-limit each host. Internal URLs must be
explicitly listed in `allowed_hosts` or set `allow_private_network: true` for a
trusted deployment.
