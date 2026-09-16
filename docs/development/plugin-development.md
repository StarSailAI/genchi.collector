# Plugin Development

Use `examples/custom-fetcher` as a minimal package.

A Fetcher defines a Pydantic configuration model, a `FetcherManifest`, and a
`fetch()` method. Backfill may reuse `fetch()` or implement `backfill()`.

```python
class MyFetcher(FetcherPlugin):
    manifest = FetcherManifest(
        name="acme.records",
        version="1.0.0",
        operations=("fetch", "backfill"),
        default_queue="web",
    )
    config_model = MyConfig

    def fetch(self, context, request):
        context.emit(ResourceRecord(
            external_id="stable-upstream-id",
            kind="article",
            title="Title",
            content="Body",
        ))
        context.set_checkpoint({"cursor": "next"})
        return FetchReport(details={"upstream_status": "ok"})
```

Use `context.secret(name)` for secrets and `context.emit_asset()` for files.
Raise typed SDK errors so the Controller can make the correct retry decision:

- `TransientError`: retry with Source backoff.
- `RateLimitError`: retry using `retry_after_seconds`.
- `AuthenticationError`: dead-letter for operator action.
- `ConfigurationError`: dead-letter until config is corrected.
- `PermanentError`: do not retry.

Fetcher code runs with Worker permissions inside a child process. The process
boundary provides hard timeouts and failure isolation, not a security sandbox.

Keep upstream IDs stable. Do not include fetch time in `external_id`; otherwise
every run creates a new Resource and defeats idempotency.
