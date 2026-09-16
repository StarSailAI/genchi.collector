# Deployment and Scaling

## Controller Host

Use a private network and TLS reverse proxy in production. Configure a strong
`CONTROL_API_TOKEN`, an external PostgreSQL 15+ database, and a read-only mounted
Source directory.

```bash
docker compose up -d --build postgres control dashboard
```

The Compose Controller applies Alembic migrations before it starts. For a
managed database, run migrations as a separate deployment step:

```bash
docker compose run --rm control allfeeds-control migrate
```

## Add a Worker

Create a one-time enrollment token on the Controller:

```bash
docker compose exec control \
  allfeeds-control enrollment-create --mode burst --ttl 3600
```

On the Worker machine configure:

```dotenv
CONTROL_URL=https://allfeeds-control.internal
DATABASE_URL=postgresql://worker:password@postgres.internal/allfeeds
ENROLLMENT_TOKEN=afe_xxx
ALLFEEDS_NODE_ID=burst-01
ALLFEEDS_WORKER_CONCURRENCY=auto
```

Then start:

```bash
docker compose -f docker-compose.worker.yml up -d --build
```

Workers need outbound access to the Controller, configured Sink, asset store,
and upstream sites. They do not need an inbound port.

Use a restricted PostgreSQL role for Workers. It should write Resource and
fetch-state tables but must not alter task/control tables.

## Scale Down

Set a temporary Worker to `draining`, wait for slot usage to reach zero, then
stop it:

```bash
curl -X PUT "$CONTROL_URL/v1/workers/$NODE_ID/state" \
  -H "X-API-Key: $CONTROL_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"state":"draining"}'
```

If a Worker disappears, stale leases return to retry automatically.
