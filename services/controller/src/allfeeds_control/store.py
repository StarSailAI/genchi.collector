from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from allfeeds_contracts import SourceSpec, TaskEnvelope, WorkerDescriptor
from psycopg.types.json import Jsonb

from .config import LoadedConfig
from .db import connection
from .scheduling import next_run
from .settings import Settings

UTC = UTC
RETRYABLE_ERRORS = {"transient", "rate_limited", "timeout", "network", "unknown"}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _task(row: dict[str, Any]) -> TaskEnvelope:
    return TaskEnvelope(
        id=row["id"],
        dedupe_key=row["dedupe_key"],
        operation=row["operation"],
        workload=row["workload"],
        source_id=row["source_id"],
        source=dict(row["source_snapshot"]),
        priority=row["priority"],
        status=row["status"],
        scheduled_for=row["scheduled_for"],
        not_before=row["not_before"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        queue=row["queue"],
        required_capabilities=tuple(row["required_capabilities"] or ()),
        resource_requirements=dict(row["resource_requirements"] or {}),
        slot_cost=row["slot_cost"],
        execution_timeout_seconds=row["execution_timeout_seconds"],
        batch_id=row.get("batch_id"),
        parent_task_id=row.get("parent_task_id"),
        window_start=row.get("window_start"),
        window_end=row.get("window_end"),
        locked_by=row.get("locked_by"),
        locked_at=row.get("locked_at"),
        lease_token=str(row["lease_token"]) if row.get("lease_token") else None,
    )


def _source_capabilities(source: SourceSpec) -> list[str]:
    values = {
        f"fetcher:{source.fetcher}:api-v1",
        f"sink:{source.sink}:api-v1",
        *source.routing.capabilities,
    }
    if source.asset_store:
        values.add(f"asset_store:{source.asset_store}:api-v1")
    return sorted(values)


class CapacityUnavailable(Exception):
    pass


class ControlStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings

    @property
    def cfg(self) -> Settings:
        if self.settings is None:
            self.settings = Settings.from_env()
        return self.settings

    def reconcile_sources(self, loaded: LoadedConfig) -> int:
        now = datetime.now(UTC)
        active: list[str] = []
        with connection(self.cfg) as conn, conn.transaction():
            for source in loaded.value.sources:
                active.append(source.id)
                spec = source.model_dump(mode="json")
                source_hash = hashlib.sha256(
                    json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()[:16]
                conn.execute(
                    """
                    INSERT INTO sources (source_id,fetcher,sink,enabled,spec,config_hash)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (source_id) DO UPDATE SET
                        fetcher=EXCLUDED.fetcher,sink=EXCLUDED.sink,
                        enabled=EXCLUDED.enabled,spec=EXCLUDED.spec,
                        config_hash=EXCLUDED.config_hash,updated_at=NOW()
                    """,
                    (
                        source.id,
                        source.fetcher,
                        source.sink,
                        source.enabled,
                        Jsonb(spec),
                        source_hash,
                    ),
                )
                existing = conn.execute(
                    "SELECT next_run_at,enabled FROM schedules WHERE schedule_id=%s",
                    (f"source:{source.id}",),
                ).fetchone()
                initial = existing["next_run_at"] if existing and existing["enabled"] else None
                if source.enabled and initial is None:
                    initial = next_run(source.schedule, now - timedelta(seconds=1))
                    if source.schedule.type == "interval" and initial:
                        spread = int(hashlib.sha256(source.id.encode()).hexdigest(), 16) % min(
                            int(source.schedule.seconds or 60), 60
                        )
                        initial = now + timedelta(seconds=spread)
                conn.execute(
                    """
                    INSERT INTO schedules (
                        schedule_id,source_id,enabled,schedule_spec,next_run_at
                    ) VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (schedule_id) DO UPDATE SET
                        enabled=EXCLUDED.enabled,schedule_spec=EXCLUDED.schedule_spec,
                        next_run_at=CASE
                            WHEN NOT EXCLUDED.enabled THEN NULL
                            WHEN NOT schedules.enabled THEN EXCLUDED.next_run_at
                            ELSE COALESCE(schedules.next_run_at,EXCLUDED.next_run_at)
                        END,updated_at=NOW()
                    """,
                    (
                        f"source:{source.id}",
                        source.id,
                        source.enabled,
                        Jsonb(source.schedule.model_dump(mode="json")),
                        initial,
                    ),
                )
            conn.execute(
                """
                UPDATE sources SET enabled=FALSE,updated_at=NOW()
                WHERE managed_by='config' AND NOT (source_id=ANY(%s))
                """,
                (active or ["__none__"],),
            )
            conn.execute(
                """
                UPDATE schedules SET enabled=FALSE,next_run_at=NULL,updated_at=NOW()
                WHERE source_id IN (SELECT source_id FROM sources WHERE NOT enabled)
                """
            )
        return len(active)

    def register_due_schedules(self, limit: int = 500) -> list[int]:
        task_ids: list[int] = []
        with connection(self.cfg) as conn, conn.transaction():
            rows = conn.execute(
                """
                SELECT sch.schedule_id,sch.next_run_at,src.spec
                  FROM schedules sch JOIN sources src USING (source_id)
                 WHERE sch.enabled AND src.enabled AND sch.active_task_id IS NULL
                   AND sch.next_run_at IS NOT NULL AND sch.next_run_at<=NOW()
                 ORDER BY sch.next_run_at LIMIT %s FOR UPDATE OF sch SKIP LOCKED
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                source = SourceSpec.model_validate(row["spec"])
                due = row["next_run_at"]
                dedupe = f"schedule:{row['schedule_id']}:{due.astimezone(UTC).isoformat()}"
                task_id = self._insert_task(
                    conn,
                    source=source,
                    dedupe_key=dedupe,
                    operation="fetch",
                    workload="scheduled",
                    scheduled_for=due,
                    not_before=due,
                    created_by="scheduler",
                    schedule_id=row["schedule_id"],
                )
                if task_id:
                    conn.execute(
                        """
                        UPDATE schedules SET active_task_id=%s,next_run_at=NULL,
                            last_registered_at=NOW(),updated_at=NOW()
                        WHERE schedule_id=%s
                        """,
                        (task_id, row["schedule_id"]),
                    )
                    conn.execute("SELECT pg_notify('allfeeds_tasks', '1')")
                    task_ids.append(task_id)
        return task_ids

    def _insert_task(
        self,
        conn,
        *,
        source: SourceSpec,
        dedupe_key: str,
        operation: str,
        workload: str,
        scheduled_for: datetime,
        not_before: datetime,
        created_by: str,
        schedule_id: str | None = None,
        batch_id: int | None = None,
        parent_task_id: int | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> int | None:
        # A task's idempotency key survives movement to terminal history.
        # Serialize registration with archival, so a just-completed task cannot
        # be executed again and then lose its report on task_runs' unique key.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("task-dedupe:" + dedupe_key,))
        if conn.execute(
            "SELECT 1 FROM tasks WHERE dedupe_key=%s UNION ALL SELECT 1 FROM task_runs WHERE dedupe_key=%s LIMIT 1",
            (dedupe_key, dedupe_key),
        ).fetchone():
            return None
        row = conn.execute(
            """
            INSERT INTO tasks (
                dedupe_key,schedule_id,batch_id,parent_task_id,operation,workload,
                source_id,source_snapshot,priority,scheduled_for,not_before,
                max_attempts,queue,required_capabilities,resource_requirements,
                slot_cost,preferred_worker,execution_timeout_seconds,
                window_start,window_end,created_by
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
            ) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id
            """,
            (
                dedupe_key,
                schedule_id,
                batch_id,
                parent_task_id,
                operation,
                workload,
                source.id,
                Jsonb(source.model_dump(mode="json")),
                source.priority,
                scheduled_for,
                not_before,
                source.retry.max_attempts,
                source.routing.queue,
                _source_capabilities(source),
                Jsonb(source.routing.resources),
                source.routing.slot_cost,
                source.routing.preferred_worker,
                source.timeout_seconds,
                window_start,
                window_end,
                created_by,
            ),
        ).fetchone()
        return int(row["id"]) if row else None

    def source(self, source_id: str) -> SourceSpec | None:
        with connection(self.cfg) as conn:
            row = conn.execute(
                "SELECT spec FROM sources WHERE source_id=%s", (source_id,)
            ).fetchone()
        return SourceSpec.model_validate(row["spec"]) if row else None

    def register_manual(
        self,
        *,
        source_id: str,
        operation: str = "fetch",
        dedupe_key: str | None = None,
        created_by: str = "control-api",
    ) -> int | None:
        source = self.source(source_id)
        if source is None:
            raise KeyError(source_id)
        now = datetime.now(UTC)
        with connection(self.cfg) as conn, conn.transaction():
            task_id = self._insert_task(
                conn,
                source=source,
                dedupe_key=dedupe_key or f"manual:{source_id}:{uuid.uuid4()}",
                operation=operation,
                workload="manual",
                scheduled_for=now,
                not_before=now,
                created_by=created_by,
            )
            if task_id:
                conn.execute("SELECT pg_notify('allfeeds_tasks', '1')")
            return task_id

    def create_backfill(
        self,
        *,
        source_id: str,
        start: datetime,
        end: datetime,
        window_seconds: int | None,
        created_by: str,
    ) -> dict[str, Any]:
        source = self.source(source_id)
        if source is None:
            raise KeyError(source_id)
        if not source.backfill_enabled:
            raise ValueError(f"source {source_id!r} does not enable backfill")
        if end <= start:
            raise ValueError("backfill end must be after start")
        window = max(60, int(window_seconds or source.backfill_window_seconds))
        with connection(self.cfg) as conn, conn.transaction():
            batch = conn.execute(
                """
                INSERT INTO task_batches (batch_type,source_id,params,created_by)
                VALUES ('backfill',%s,%s,%s) RETURNING id
                """,
                (
                    source_id,
                    Jsonb(
                        {
                            "start": start.isoformat(),
                            "end": end.isoformat(),
                            "window_seconds": window,
                        }
                    ),
                    created_by,
                ),
            ).fetchone()
            batch_id = int(batch["id"])
            cursor, task_ids = start, []
            while cursor < end:
                window_end = min(end, cursor + timedelta(seconds=window))
                dedupe = f"backfill:{batch_id}:{cursor.isoformat()}:{window_end.isoformat()}"
                task_id = self._insert_task(
                    conn,
                    source=source,
                    dedupe_key=dedupe,
                    operation="backfill",
                    workload="backfill",
                    scheduled_for=datetime.now(UTC),
                    not_before=datetime.now(UTC),
                    created_by=created_by,
                    batch_id=batch_id,
                    window_start=cursor,
                    window_end=window_end,
                )
                if task_id:
                    task_ids.append(task_id)
                cursor = window_end
            conn.execute(
                "UPDATE task_batches SET total_tasks=%s WHERE id=%s",
                (len(task_ids), batch_id),
            )
            if task_ids:
                conn.execute("SELECT pg_notify('allfeeds_tasks', %s)", (str(len(task_ids)),))
        return {"batch_id": batch_id, "task_count": len(task_ids)}

    def backfill(self, batch_id: int) -> dict[str, Any] | None:
        with connection(self.cfg) as conn:
            row = conn.execute(
                "SELECT * FROM task_batches WHERE id=%s AND batch_type='backfill'",
                (batch_id,),
            ).fetchone()
            return dict(row) if row else None

    def set_backfill_state(self, batch_id: int, action: str) -> dict[str, Any] | None:
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError(action)
        state = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[action]
        with connection(self.cfg) as conn, conn.transaction():
            row = conn.execute(
                """
                UPDATE task_batches SET status=%s,updated_at=NOW(),
                    completed_at=CASE WHEN %s='cancelled' THEN NOW() ELSE completed_at END
                WHERE id=%s AND batch_type='backfill'
                  AND status IN ('running','paused') RETURNING *
                """,
                (state, state, batch_id),
            ).fetchone()
            if not row:
                return None
            if action == "cancel":
                conn.execute(
                    "DELETE FROM tasks WHERE batch_id=%s AND status IN ('pending','retry')",
                    (batch_id,),
                )
            elif action == "resume":
                conn.execute("SELECT pg_notify('allfeeds_tasks', '1')")
            return dict(row)

    def create_enrollment(self, *, mode: str, ttl_seconds: int, max_uses: int, created_by: str):
        token = _token("afe")
        with connection(self.cfg) as conn, conn.transaction():
            conn.execute(
                """
                INSERT INTO worker_enrollments
                    (token_hash,mode,max_uses,expires_at,created_by)
                VALUES (%s,%s,%s,NOW()+(%s * INTERVAL '1 second'),%s)
                """,
                (_hash(token), mode, max_uses, ttl_seconds, created_by),
            )
        return {"token": token, "mode": mode, "expires_in_seconds": ttl_seconds}

    def bootstrap_enrollment(self, token: str, *, max_uses: int) -> None:
        if len(token) < 20:
            raise ValueError("bootstrap enrollment token must contain at least 20 characters")
        with connection(self.cfg) as conn, conn.transaction():
            conn.execute(
                """
                INSERT INTO worker_enrollments
                    (token_hash,mode,max_uses,expires_at,created_by)
                VALUES (%s,'resident',%s,NOW()+INTERVAL '10 years','compose-bootstrap')
                ON CONFLICT (token_hash) DO UPDATE SET
                    max_uses=EXCLUDED.max_uses,expires_at=EXCLUDED.expires_at,revoked_at=NULL
                """,
                (_hash(token), max_uses),
            )

    def enroll_worker(
        self, enrollment_token: str, descriptor: WorkerDescriptor
    ) -> dict[str, Any] | None:
        credential = _token("afw")
        with connection(self.cfg) as conn, conn.transaction():
            enrollment = conn.execute(
                """
                SELECT * FROM worker_enrollments
                WHERE token_hash=%s AND revoked_at IS NULL AND expires_at>NOW() AND uses<max_uses
                FOR UPDATE
                """,
                (_hash(enrollment_token),),
            ).fetchone()
            if not enrollment:
                return None
            mode = enrollment["mode"]
            conn.execute(
                """
                INSERT INTO workers (
                    node_id,instance_id,hostname,mode,max_concurrency,capabilities,
                    queues,plugins,software_version,metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (node_id) DO UPDATE SET
                    instance_id=EXCLUDED.instance_id,hostname=EXCLUDED.hostname,
                    mode=EXCLUDED.mode,desired_state='online',effective_state='online',
                    max_concurrency=EXCLUDED.max_concurrency,
                    capabilities=EXCLUDED.capabilities,queues=EXCLUDED.queues,
                    plugins=EXCLUDED.plugins,software_version=EXCLUDED.software_version,
                    metadata=EXCLUDED.metadata,last_heartbeat_at=NOW(),updated_at=NOW()
                """,
                (
                    descriptor.node_id,
                    descriptor.instance_id,
                    descriptor.hostname,
                    mode,
                    descriptor.max_concurrency,
                    list(descriptor.capabilities),
                    list(descriptor.queues),
                    Jsonb([p.model_dump(mode="json") for p in descriptor.plugins]),
                    descriptor.software_version,
                    Jsonb(descriptor.metadata),
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_credentials (node_id,credential_hash)
                VALUES (%s,%s) ON CONFLICT (node_id) DO UPDATE SET
                    credential_hash=EXCLUDED.credential_hash,revoked_at=NULL,created_at=NOW()
                """,
                (descriptor.node_id, _hash(credential)),
            )
            conn.execute(
                "UPDATE worker_enrollments SET uses=uses+1 WHERE id=%s", (enrollment["id"],)
            )
        return {"credential": credential, "node_id": descriptor.node_id, "mode": mode}

    def authenticate_worker(self, credential: str) -> dict[str, Any] | None:
        with connection(self.cfg) as conn, conn.transaction():
            row = conn.execute(
                """
                SELECT w.* FROM worker_credentials c JOIN workers w USING (node_id)
                WHERE c.credential_hash=%s AND c.revoked_at IS NULL
                """,
                (_hash(credential),),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE worker_credentials SET last_used_at=NOW() WHERE node_id=%s",
                    (row["node_id"],),
                )
            return dict(row) if row else None

    def start_worker(self, descriptor: WorkerDescriptor) -> bool:
        with connection(self.cfg) as conn, conn.transaction():
            row = conn.execute(
                """
                UPDATE workers SET instance_id=%s,hostname=%s,max_concurrency=%s,
                    capabilities=%s,queues=%s,plugins=%s,software_version=%s,
                    metadata=%s,effective_state='online',last_heartbeat_at=NOW(),updated_at=NOW()
                WHERE node_id=%s AND desired_state<>'disabled' RETURNING node_id
                """,
                (
                    descriptor.instance_id,
                    descriptor.hostname,
                    descriptor.max_concurrency,
                    list(descriptor.capabilities),
                    list(descriptor.queues),
                    Jsonb([p.model_dump(mode="json") for p in descriptor.plugins]),
                    descriptor.software_version,
                    Jsonb(descriptor.metadata),
                    descriptor.node_id,
                ),
            ).fetchone()
            return bool(row)

    def claim_tasks(
        self, *, node_id: str, instance_id: str, available_slots: int
    ) -> tuple[list[TaskEnvelope], str]:
        claimed: list[TaskEnvelope] = []
        with connection(self.cfg) as conn, conn.transaction():
            worker = conn.execute(
                "SELECT * FROM workers WHERE node_id=%s FOR UPDATE", (node_id,)
            ).fetchone()
            if not worker or worker["instance_id"] != instance_id:
                return [], "replaced"
            desired = worker["desired_state"]
            if desired != "online" or available_slots <= 0:
                return [], desired
            conn.execute("DELETE FROM resource_leases WHERE expires_at<=NOW()")
            capabilities = set(worker["capabilities"] or ())
            queues = list(worker["queues"] or ("default",))
            running_backfill = conn.execute(
                """
                SELECT COALESCE(SUM(slot_cost),0) AS slots FROM tasks
                WHERE locked_by=%s AND status='running' AND workload='backfill'
                """,
                (node_id,),
            ).fetchone()["slots"]
            candidates = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN ('pending','retry') AND not_before<=NOW()
                  AND queue=ANY(%s)
                  AND (preferred_worker IS NULL OR preferred_worker=%s)
                  AND required_capabilities <@ %s::text[]
                  AND (batch_id IS NULL OR EXISTS (
                      SELECT 1 FROM task_batches b
                      WHERE b.id=tasks.batch_id AND b.status='running'
                  ))
                ORDER BY priority,not_before,id
                LIMIT 200 FOR UPDATE SKIP LOCKED
                """,
                (queues, node_id, sorted(capabilities)),
            ).fetchall()
            remaining = available_slots
            for candidate in candidates:
                cost = int(candidate["slot_cost"])
                if cost > remaining:
                    continue
                if (
                    worker["mode"] == "resident"
                    and candidate["workload"] == "backfill"
                    and running_backfill + cost > self.cfg.resident_backfill_slots
                ):
                    continue
                lease = uuid.uuid4()
                try:
                    with conn.transaction():
                        self._acquire_resources(
                            conn,
                            candidate["id"],
                            lease,
                            node_id,
                            dict(candidate["resource_requirements"] or {}),
                        )
                except CapacityUnavailable:
                    continue
                row = conn.execute(
                    """
                    UPDATE tasks SET status='running',attempts=attempts+1,locked_by=%s,
                        locked_at=NOW(),heartbeat_at=NOW(),lease_token=%s,updated_at=NOW()
                    WHERE id=%s RETURNING *
                    """,
                    (node_id, lease, candidate["id"]),
                ).fetchone()
                claimed.append(_task(row))
                remaining -= cost
                if candidate["workload"] == "backfill":
                    running_backfill += cost
                if remaining <= 0:
                    break
        return claimed, desired

    def _acquire_resources(
        self,
        conn,
        task_id: int,
        lease_token: uuid.UUID,
        worker_id: str,
        requirements: dict[str, int],
    ) -> None:
        ttl = max(300, self.cfg.task_stale_seconds + 60)
        for resource_key, capacity in sorted(requirements.items()):
            row = conn.execute(
                """
                SELECT candidate_slots.slot
                FROM generate_series(1,%s) AS candidate_slots(slot)
                WHERE NOT EXISTS (
                    SELECT 1 FROM resource_leases r
                    WHERE r.resource_key=%s
                      AND r.slot=candidate_slots.slot
                      AND r.expires_at>NOW()
                ) ORDER BY candidate_slots.slot LIMIT 1
                """,
                (max(1, int(capacity)), resource_key),
            ).fetchone()
            if not row:
                raise CapacityUnavailable(resource_key)
            conn.execute(
                """
                INSERT INTO resource_leases
                    (resource_key,slot,task_id,lease_token,worker_id,expires_at)
                VALUES (%s,%s,%s,%s,%s,NOW()+(%s * INTERVAL '1 second'))
                """,
                (resource_key, row["slot"], task_id, lease_token, worker_id, ttl),
            )

    def heartbeat(
        self,
        *,
        node_id: str,
        instance_id: str,
        running: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> str:
        ttl = max(300, self.cfg.task_stale_seconds + 60)
        with connection(self.cfg) as conn, conn.transaction():
            worker = conn.execute(
                "SELECT desired_state,instance_id FROM workers WHERE node_id=%s FOR UPDATE",
                (node_id,),
            ).fetchone()
            if not worker or worker["instance_id"] != instance_id:
                return "replaced"
            conn.execute(
                """
                UPDATE workers SET effective_state=desired_state,metadata=%s,
                    last_heartbeat_at=NOW(),updated_at=NOW() WHERE node_id=%s
                """,
                (Jsonb(metadata), node_id),
            )
            for lease in running:
                token = lease.get("lease_token")
                task_id = lease.get("task_id")
                conn.execute(
                    """
                    UPDATE tasks SET heartbeat_at=NOW(),updated_at=NOW()
                    WHERE id=%s AND locked_by=%s AND lease_token=%s AND status='running'
                    """,
                    (task_id, node_id, token),
                )
                conn.execute(
                    """
                    UPDATE resource_leases SET expires_at=NOW()+(%s * INTERVAL '1 second')
                    WHERE task_id=%s AND worker_id=%s AND lease_token=%s
                    """,
                    (ttl, task_id, node_id, token),
                )
            return worker["desired_state"]

    def complete_task(
        self,
        *,
        task_id: int,
        lease_token: str,
        status: str,
        report: dict[str, Any],
        error_message: str | None,
    ) -> bool:
        with connection(self.cfg) as conn, conn.transaction():
            row = conn.execute("SELECT * FROM tasks WHERE id=%s FOR UPDATE", (task_id,)).fetchone()
            if not row or str(row["lease_token"]) != lease_token or row["status"] != "running":
                return False
            self._archive(conn, row, status, report, None, error_message)
            self._finish_schedule(conn, row)
            self._finish_batch(conn, row["batch_id"], failed=status == "partial")
            conn.execute("DELETE FROM resource_leases WHERE task_id=%s", (task_id,))
            conn.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
            return True

    def fail_task(
        self,
        *,
        task_id: int,
        lease_token: str,
        error_class: str,
        error_message: str,
        retry_after_seconds: float | None = None,
    ) -> dict[str, Any]:
        with connection(self.cfg) as conn, conn.transaction():
            row = conn.execute("SELECT * FROM tasks WHERE id=%s FOR UPDATE", (task_id,)).fetchone()
            if not row or str(row["lease_token"]) != lease_token or row["status"] != "running":
                return {"accepted": False, "reason": "lease_lost"}
            source = SourceSpec.model_validate(row["source_snapshot"])
            retryable = error_class in RETRYABLE_ERRORS and row["attempts"] < row["max_attempts"]
            conn.execute("DELETE FROM resource_leases WHERE task_id=%s", (task_id,))
            if retryable:
                if retry_after_seconds is not None:
                    delay = min(source.retry.max_seconds, max(0, retry_after_seconds))
                elif source.retry.backoff == "exponential":
                    delay = min(
                        source.retry.max_seconds,
                        source.retry.base_seconds * (2 ** max(0, row["attempts"] - 1)),
                    )
                else:
                    delay = source.retry.base_seconds
                conn.execute(
                    """
                    UPDATE tasks SET status='retry',not_before=NOW()+(%s * INTERVAL '1 second'),
                        locked_by=NULL,locked_at=NULL,heartbeat_at=NULL,lease_token=NULL,
                        last_error_class=%s,last_error=%s,updated_at=NOW() WHERE id=%s
                    """,
                    (delay, error_class, error_message[:16_000], task_id),
                )
                conn.execute("SELECT pg_notify('allfeeds_tasks', '1')")
                return {"accepted": True, "action": "retry", "delay_seconds": delay}
            self._archive(conn, row, "dead", {}, error_class, error_message)
            self._finish_schedule(conn, row)
            self._finish_batch(conn, row["batch_id"], failed=True)
            conn.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
            return {"accepted": True, "action": "dead"}

    @staticmethod
    def _archive(
        conn,
        row: dict[str, Any],
        final_status: str,
        result: dict[str, Any],
        error_class: str | None,
        error_message: str | None,
    ) -> None:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("task-dedupe:" + row["dedupe_key"],))
        now = datetime.now(UTC)
        duration = (now - row["locked_at"]).total_seconds() if row["locked_at"] else None
        conn.execute(
            """
            INSERT INTO task_runs (
                task_id,dedupe_key,schedule_id,batch_id,operation,workload,source_id,
                source_snapshot,final_status,attempts,worker_id,lease_token,rows_added,
                result,error_class,error_message,scheduled_for,started_at,duration_seconds
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (dedupe_key) DO NOTHING
            """,
            (
                row["id"],
                row["dedupe_key"],
                row["schedule_id"],
                row["batch_id"],
                row["operation"],
                row["workload"],
                row["source_id"],
                Jsonb(row["source_snapshot"]),
                final_status,
                row["attempts"],
                row["locked_by"],
                row["lease_token"],
                int(result.get("added", 0)),
                Jsonb(result),
                error_class,
                error_message[:16_000] if error_message else None,
                row["scheduled_for"],
                row["locked_at"],
                duration,
            ),
        )

    @staticmethod
    def _finish_schedule(conn, task: dict[str, Any]) -> None:
        if not task["schedule_id"]:
            return
        source = SourceSpec.model_validate(task["source_snapshot"])
        following = next_run(source.schedule, datetime.now(UTC))
        conn.execute(
            """
            UPDATE schedules SET active_task_id=NULL,next_run_at=%s,
                last_finished_at=NOW(),enabled=(enabled AND %s),updated_at=NOW()
            WHERE schedule_id=%s
            """,
            (following, following is not None, task["schedule_id"]),
        )

    @staticmethod
    def _finish_batch(conn, batch_id: int | None, *, failed: bool) -> None:
        if batch_id is None:
            return
        conn.execute(
            """
            UPDATE task_batches SET completed_tasks=completed_tasks+1,
                failed_tasks=failed_tasks+%s,updated_at=NOW(),
                status=CASE
                    WHEN completed_tasks+1>=total_tasks
                        THEN CASE WHEN failed_tasks+%s>0 THEN 'partial' ELSE 'completed' END
                    ELSE status END,
                completed_at=CASE WHEN completed_tasks+1>=total_tasks THEN NOW() ELSE NULL END
            WHERE id=%s
            """,
            (1 if failed else 0, 1 if failed else 0, batch_id),
        )

    def maintenance(self) -> dict[str, int]:
        with connection(self.cfg) as conn, conn.transaction():
            offline = conn.execute(
                """
                UPDATE workers SET effective_state='offline',updated_at=NOW()
                WHERE last_heartbeat_at<NOW()-(%s * INTERVAL '1 second')
                  AND effective_state<>'offline' RETURNING node_id
                """,
                (self.cfg.node_offline_seconds,),
            ).fetchall()
            stale = conn.execute(
                """
                SELECT id FROM tasks WHERE status='running'
                  AND heartbeat_at<NOW()-(%s * INTERVAL '1 second') FOR UPDATE SKIP LOCKED
                """,
                (self.cfg.task_stale_seconds,),
            ).fetchall()
            for item in stale:
                conn.execute("DELETE FROM resource_leases WHERE task_id=%s", (item["id"],))
                conn.execute(
                    """
                    UPDATE tasks SET status='retry',not_before=NOW()+INTERVAL '15 seconds',
                        locked_by=NULL,locked_at=NULL,heartbeat_at=NULL,lease_token=NULL,
                        last_error_class='stale_lease',last_error='worker heartbeat expired',
                        updated_at=NOW() WHERE id=%s
                    """,
                    (item["id"],),
                )
            conn.execute("DELETE FROM resource_leases WHERE expires_at<=NOW()")
        return {"offline_workers": len(offline), "stale_tasks": len(stale)}

    def set_worker_state(self, node_id: str, state: str) -> bool:
        with connection(self.cfg) as conn, conn.transaction():
            row = conn.execute(
                "UPDATE workers SET desired_state=%s,updated_at=NOW() WHERE node_id=%s RETURNING node_id",
                (state, node_id),
            ).fetchone()
            return bool(row)

    def revoke_worker(self, node_id: str) -> bool:
        with connection(self.cfg) as conn, conn.transaction():
            row = conn.execute(
                """
                UPDATE worker_credentials SET revoked_at=NOW()
                WHERE node_id=%s AND revoked_at IS NULL RETURNING node_id
                """,
                (node_id,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE workers SET desired_state='disabled',updated_at=NOW() WHERE node_id=%s",
                    (node_id,),
                )
            return bool(row)

    def sources(self) -> list[dict[str, Any]]:
        with connection(self.cfg) as conn:
            return conn.execute(
                """
                SELECT source_id,fetcher,sink,enabled,config_hash,managed_by,
                       created_at,updated_at FROM sources ORDER BY source_id
                """
            ).fetchall()

    def overview(self) -> dict[str, Any]:
        with connection(self.cfg) as conn:
            counts = conn.execute(
                "SELECT status,COUNT(*) AS count FROM tasks GROUP BY status"
            ).fetchall()
            workers = conn.execute(
                """
                SELECT w.*,
                    COALESCE((SELECT SUM(t.slot_cost) FROM tasks t
                              WHERE t.locked_by=w.node_id AND t.status='running'),0) AS running_slots
                FROM workers w ORDER BY last_heartbeat_at DESC
                """
            ).fetchall()
            queue = conn.execute(
                """
                SELECT id,status,operation,workload,source_id,priority,not_before,
                       attempts,max_attempts,queue,slot_cost,last_error_class
                FROM tasks WHERE status IN ('pending','retry')
                ORDER BY priority,not_before,id LIMIT 100
                """
            ).fetchall()
            running = conn.execute(
                """
                SELECT id,operation,workload,source_id,priority,attempts,max_attempts,
                       locked_by,locked_at,heartbeat_at,slot_cost
                FROM tasks WHERE status='running' ORDER BY locked_at LIMIT 100
                """
            ).fetchall()
            errors = conn.execute(
                """
                SELECT task_id,source_id,final_status,attempts,worker_id,error_class,
                       error_message,finished_at FROM task_runs
                WHERE final_status IN ('partial','dead') ORDER BY finished_at DESC LIMIT 200
                """
            ).fetchall()
            recent = conn.execute(
                """
                SELECT task_id,source_id,operation,workload,final_status,attempts,
                       worker_id,rows_added,result,finished_at,duration_seconds
                FROM task_runs ORDER BY finished_at DESC LIMIT 200
                """
            ).fetchall()
            schedules = conn.execute(
                """
                SELECT s.source_id,s.enabled,s.schedule_spec,s.next_run_at,s.last_finished_at,
                       src.fetcher,src.sink FROM schedules s JOIN sources src USING(source_id)
                ORDER BY s.enabled DESC,s.next_run_at NULLS LAST LIMIT 500
                """
            ).fetchall()
            resources = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE observed_at>=NOW()-INTERVAL '24 hours') AS recent_24h,
                       MAX(observed_at) AS latest_at FROM resources
                """
            ).fetchone()
            kinds = conn.execute(
                "SELECT kind,COUNT(*) AS count,MAX(observed_at) AS latest_at FROM resources GROUP BY kind ORDER BY count DESC LIMIT 20"
            ).fetchall()
            success = conn.execute(
                """
                SELECT COUNT(*) AS completed,
                    COUNT(*) FILTER (WHERE final_status='succeeded') AS succeeded,
                    COUNT(*) FILTER (WHERE final_status IN ('partial','dead')) AS issues,
                    COALESCE(SUM(rows_added),0) AS rows_added
                FROM task_runs WHERE finished_at>=NOW()-INTERVAL '24 hours'
                """
            ).fetchone()
            genchi = {
                "normalization": {},
                "content_total": 0,
                "candidates_pending": 0,
            }
            if conn.execute(
                "SELECT to_regclass('genchi.\"NormalizationJob\"') AS value"
            ).fetchone()["value"]:
                normalization = conn.execute(
                    'SELECT "status",COUNT(*) AS count FROM genchi."NormalizationJob" GROUP BY "status"'
                ).fetchall()
                genchi = {
                    "normalization": {
                        row["status"]: row["count"] for row in normalization
                    },
                    "content_total": conn.execute(
                        'SELECT COUNT(*) AS count FROM genchi."ContentItem" WHERE "searchable"'
                    ).fetchone()["count"],
                    "candidates_pending": conn.execute(
                        'SELECT COUNT(*) AS count FROM genchi."ExtractionCandidate" WHERE "status"=\'PENDING\''
                    ).fetchone()["count"],
                }
        return {
            "generated_at": datetime.now(UTC),
            "task_counts": {row["status"]: row["count"] for row in counts},
            "workers": workers,
            "queue": queue,
            "running": running,
            "errors": errors,
            "recent_runs": recent,
            "schedules": schedules,
            "resources": resources,
            "resource_kinds": kinds,
            "last_24h": success,
            "genchi": genchi,
        }

    def stuck_tasks(self) -> list[dict[str, Any]]:
        with connection(self.cfg) as conn:
            return conn.execute(
                """
                SELECT t.id,t.source_id,t.required_capabilities,t.queue,t.last_error_class,
                       CASE
                         WHEN cardinality(t.required_capabilities)=0 THEN 'unroutable'
                         WHEN NOT EXISTS (
                           SELECT 1 FROM workers w WHERE w.effective_state='online'
                             AND t.required_capabilities <@ w.capabilities AND t.queue=ANY(w.queues)
                         ) THEN 'no_capable_worker'
                         ELSE NULL END AS reason
                FROM tasks t WHERE t.status IN ('pending','retry')
                AND (
                    cardinality(t.required_capabilities)=0 OR NOT EXISTS (
                        SELECT 1 FROM workers w WHERE w.effective_state='online'
                          AND t.required_capabilities <@ w.capabilities AND t.queue=ANY(w.queues)
                    )
                ) ORDER BY t.created_at LIMIT 200
                """
            ).fetchall()
