from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
import uvicorn
from alembic import command
from alembic.config import Config

from .config import load_sources
from .db import ensure_schema
from .settings import Settings
from .store import ControlStore


def _alembic_config() -> Config:
    source_root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("ALLFEEDS_ALEMBIC_ROOT", "").strip()
    if configured:
        root = Path(configured)
    elif (source_root / "alembic.ini").exists():
        root = source_root
    else:
        root = Path(sys.prefix) / "allfeeds_control_migrations"
    if not (root / "alembic.ini").exists():
        raise RuntimeError(f"Alembic migration files were not found under {root}")
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return config


def main() -> None:
    parser = argparse.ArgumentParser(prog="allfeeds-control")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8060)
    sub.add_parser("migrate")
    config_validate = sub.add_parser("config-validate")
    config_validate.add_argument("--sources", type=Path)
    enrollment = sub.add_parser("enrollment-create")
    enrollment.add_argument("--mode", choices=["resident", "burst"], default="burst")
    enrollment.add_argument("--ttl", type=int, default=1800)
    enrollment.add_argument("--max-uses", type=int, default=1)
    enrollment_bootstrap = sub.add_parser("enrollment-bootstrap")
    enrollment_bootstrap.add_argument("--token", required=True)
    enrollment_bootstrap.add_argument("--max-uses", type=int, default=100000)
    task = sub.add_parser("task-submit")
    task.add_argument("--source", required=True)
    task.add_argument("--dedupe-key")
    backfill = sub.add_parser("backfill-create")
    backfill.add_argument("--source", required=True)
    backfill.add_argument("--start", required=True)
    backfill.add_argument("--end", required=True)
    backfill.add_argument("--window-seconds", type=int)
    backfill_status = sub.add_parser("backfill-status")
    backfill_status.add_argument("batch_id", type=int)
    backfill_action = sub.add_parser("backfill-action")
    backfill_action.add_argument("batch_id", type=int)
    backfill_action.add_argument("action", choices=["pause", "resume", "cancel"])
    worker_state = sub.add_parser("worker-state")
    worker_state.add_argument("node_id")
    worker_state.add_argument("state", choices=["online", "draining", "disabled"])
    args = parser.parse_args()

    if args.command == "config-validate":
        path = args.sources or Path(os.environ.get("ALLFEEDS_SOURCES", "/app/config/sources.yaml"))
        loaded = load_sources(path)
        print(
            json.dumps(
                {
                    "valid": True,
                    "sources": len(loaded.value.sources),
                    "version": loaded.version_hash,
                }
            )
        )
        return

    if args.command == "serve":
        uvicorn.run("allfeeds_control.api:app", host=args.host, port=args.port)
        return
    if args.command in {
        "task-submit",
        "backfill-create",
        "backfill-status",
        "backfill-action",
        "worker-state",
    }:
        base = os.environ.get("CONTROL_URL", "http://127.0.0.1:8060").rstrip("/")
        token = os.environ.get("CONTROL_API_TOKEN", "")
        if not token:
            raise SystemExit("CONTROL_API_TOKEN is required")
        headers = {"X-API-Key": token}
        if args.command == "task-submit":
            response = requests.post(
                f"{base}/v1/tasks",
                headers=headers,
                json={"source_id": args.source, "dedupe_key": args.dedupe_key},
                timeout=30,
            )
        elif args.command == "backfill-create":
            response = requests.post(
                f"{base}/v1/backfills",
                headers=headers,
                json={
                    "source_id": args.source,
                    "start": args.start,
                    "end": args.end,
                    "window_seconds": args.window_seconds,
                },
                timeout=30,
            )
        elif args.command == "worker-state":
            response = requests.put(
                f"{base}/v1/workers/{args.node_id}/state",
                headers=headers,
                json={"state": args.state},
                timeout=30,
            )
        elif args.command == "backfill-status":
            response = requests.get(
                f"{base}/v1/backfills/{args.batch_id}", headers=headers, timeout=30
            )
        else:
            response = requests.post(
                f"{base}/v1/backfills/{args.batch_id}/{args.action}",
                headers=headers,
                timeout=30,
            )
        response.raise_for_status()
        print(json.dumps(response.json()))
        return

    settings = Settings.from_env()
    if args.command == "migrate":
        ensure_schema(settings)
        command.upgrade(_alembic_config(), "head")
    elif args.command == "enrollment-create":
        value = ControlStore(settings).create_enrollment(
            mode=args.mode, ttl_seconds=args.ttl, max_uses=args.max_uses, created_by="cli"
        )
        print(json.dumps(value))
    elif args.command == "enrollment-bootstrap":
        ControlStore(settings).bootstrap_enrollment(args.token, max_uses=args.max_uses)
        print(json.dumps({"bootstrapped": True}))


if __name__ == "__main__":
    main()
