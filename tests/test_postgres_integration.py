from __future__ import annotations

import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from allfeeds_contracts import PluginInventory, ResourceRecord, WorkerDescriptor
from allfeeds_control.config import load_sources
from allfeeds_control.db import close_pool, ensure_schema
from allfeeds_control.settings import Settings
from allfeeds_control.store import ControlStore
from allfeeds_worker.sink import PostgresSink

TEST_DSN = os.environ.get("ALLFEEDS_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="ALLFEEDS_TEST_DATABASE_URL is not set")


def _worker(store: ControlStore, index: int, plugins) -> WorkerDescriptor:
    value = WorkerDescriptor(
        node_id=f"test-burst-{index}",
        instance_id=f"test-instance-{index}-00000000",
        hostname="localhost",
        mode="burst",
        max_concurrency=4,
        software_version="test",
        capabilities=tuple(plugin.capability for plugin in plugins),
        queues=("default", "web"),
        plugins=plugins,
    )
    token = store.create_enrollment(mode="burst", ttl_seconds=600, max_uses=1, created_by="pytest")[
        "token"
    ]
    assert store.enroll_worker(token, value)
    assert store.start_worker(value)
    return value


def test_migration_claim_sink_and_backfill(monkeypatch, tmp_path: Path) -> None:
    schema = f"allfeeds_test_{secrets.token_hex(6)}"
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("ALLFEEDS_DB_SCHEMA", schema)
    monkeypatch.setenv("CONTROL_API_TOKEN", "test")
    source_file = tmp_path / "sources.yaml"
    source_file.write_text("""version: 1
sources:
  - id: test-official
    enabled: true
    fetcher: builtin.web_page
    sink: postgres
    schedule: {type: interval, seconds: 3600}
    backfill_enabled: true
    config: {url: 'https://example.com/'}
    routing: {queue: web, resources: {domain:example.com: 2}}
""")
    monkeypatch.setenv("ALLFEEDS_SOURCES", str(source_file))
    settings = Settings.from_env()
    try:
        ensure_schema(settings)
        alembic = Config(str(Path(__file__).parents[1] / "services/controller/alembic.ini"))
        alembic.set_main_option(
            "script_location", str(Path(__file__).parents[1] / "services/controller/alembic")
        )
        command.upgrade(alembic, "head")

        store = ControlStore(settings)
        source_config = load_sources(settings.config_path)
        assert store.reconcile_sources(source_config) == len(source_config.value.sources)
        plugins = (
            PluginInventory(
                name="builtin.web_page",
                kind="fetcher",
                version="0.1.0",
                operations=("fetch", "backfill"),
                default_queue="web",
            ),
            PluginInventory(name="postgres", kind="sink", version="0.1.0"),
        )
        first_worker = _worker(store, 1, plugins)
        second_worker = _worker(store, 2, plugins)

        manual_id = store.register_manual(source_id="test-official", dedupe_key="immutable-manual")
        assert store.register_manual(source_id="test-official", dedupe_key="immutable-manual") is None
        tasks, _ = store.claim_tasks(
            node_id=first_worker.node_id,
            instance_id=first_worker.instance_id,
            available_slots=4,
        )
        assert [task.id for task in tasks] == [manual_id]
        sink = PostgresSink(TEST_DSN, schema)
        assert (
            sink.write_records(
                "test-official",
                [ResourceRecord(external_id="one", title="One", content="Real content")],
            )["added"]
            == 1
        )
        assert store.complete_task(
            task_id=tasks[0].id,
            lease_token=tasks[0].lease_token or "",
            status="succeeded",
            report={"seen": 1, "added": 1},
            error_message=None,
        )
        with ThreadPoolExecutor(max_workers=4) as executor:
            replayed = list(executor.map(lambda _: store.register_manual(source_id="test-official", dedupe_key="immutable-manual"), range(4)))
        assert replayed == [None] * 4
        with psycopg.connect(TEST_DSN, options=f"-c search_path={schema},public") as conn:
            assert conn.execute("SELECT count(*) FROM tasks WHERE dedupe_key='immutable-manual'").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM task_runs WHERE dedupe_key='immutable-manual'").fetchone()[0] == 1

        start = datetime(2026, 7, 1, tzinfo=UTC)
        batch = store.create_backfill(
            source_id="test-official",
            start=start,
            end=start + timedelta(days=3),
            window_seconds=86_400,
            created_by="pytest",
        )
        assert batch["task_count"] == 3
        first_claim, _ = store.claim_tasks(
            node_id=first_worker.node_id,
            instance_id=first_worker.instance_id,
            available_slots=4,
        )
        assert len(first_claim) == 2  # domain resource capacity is two
        blocked, _ = store.claim_tasks(
            node_id=second_worker.node_id,
            instance_id=second_worker.instance_id,
            available_slots=4,
        )
        assert blocked == []
        assert store.complete_task(
            task_id=first_claim[0].id,
            lease_token=first_claim[0].lease_token or "",
            status="succeeded",
            report={},
            error_message=None,
        )
        released, _ = store.claim_tasks(
            node_id=second_worker.node_id,
            instance_id=second_worker.instance_id,
            available_slots=4,
        )
        assert len(released) == 1
    finally:
        close_pool()
        if TEST_DSN:
            with psycopg.connect(TEST_DSN, autocommit=True) as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
