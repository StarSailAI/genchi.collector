# HTTP API

Admin endpoints use `X-API-Key`. Worker endpoints use the credential returned by
enrollment as a Bearer token.

## Admin

- `POST /v1/enrollments`
- `POST /v1/tasks`
- `POST /v1/backfills`
- `GET /v1/backfills/{batch_id}`
- `POST /v1/backfills/{batch_id}/pause|resume|cancel`
- `GET /v1/overview`
- `GET /v1/sources`
- `GET /v1/tasks/stuck`
- `PUT /v1/workers/{node_id}/state`
- `DELETE /v1/workers/{node_id}/credential`
- `GET /metrics`

## Worker Protocol

- `POST /v1/workers/enroll`
- `POST /v1/workers/start`
- `GET /v1/workers/bootstrap`
- `POST /v1/workers/claim`
- `POST /v1/workers/heartbeat`
- `POST /v1/tasks/{task_id}/complete`
- `POST /v1/tasks/{task_id}/fail`

The protocol version is independent from individual plugin versions. Tasks
require an SDK API capability such as `fetcher:builtin.rss:api-v1`.
