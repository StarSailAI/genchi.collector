from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

import psutil


def _auto_concurrency() -> int:
    cpu = max(1, psutil.cpu_count(logical=True) or 1)
    memory_gb = psutil.virtual_memory().total / (1024**3)
    return max(1, min(64, cpu, int(memory_gb / 1.5)))


@dataclass(frozen=True)
class WorkerSettings:
    control_url: str
    enrollment_token: str | None
    database_url: str
    database_schema: str
    state_path: Path
    asset_path: Path
    node_id: str
    hostname: str
    concurrency: int
    heartbeat_seconds: float
    request_timeout_seconds: float
    tls_verify: bool | str
    software_version: str

    @classmethod
    def from_env(cls) -> WorkerSettings:
        control_url = os.environ.get("CONTROL_URL", "").strip().rstrip("/")
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not control_url:
            raise RuntimeError("CONTROL_URL is required")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required for the built-in PostgreSQL sink")
        raw_concurrency = os.environ.get("ALLFEEDS_WORKER_CONCURRENCY", "auto").strip()
        concurrency = _auto_concurrency() if raw_concurrency == "auto" else int(raw_concurrency)
        verify_raw = os.environ.get("CONTROL_TLS_VERIFY", "1").strip()
        verify: bool | str = verify_raw.lower() not in {"0", "false", "no", "off"}
        if verify and verify_raw not in {"1", "true", "yes", "on"}:
            verify = verify_raw
        hostname = socket.gethostname()
        return cls(
            control_url=control_url,
            enrollment_token=os.environ.get("ENROLLMENT_TOKEN", "").strip() or None,
            database_url=database_url,
            database_schema=os.environ.get("ALLFEEDS_DB_SCHEMA", "allfeeds"),
            state_path=Path(
                os.environ.get("WORKER_STATE_PATH", "/var/lib/allfeeds-worker/identity.json")
            ),
            asset_path=Path(os.environ.get("ASSET_STORE_PATH", "/data/assets")),
            node_id=os.environ.get("ALLFEEDS_NODE_ID", "").strip() or hostname,
            hostname=hostname,
            concurrency=max(1, min(128, concurrency)),
            heartbeat_seconds=max(2, float(os.environ.get("HEARTBEAT_SECONDS", "15"))),
            request_timeout_seconds=max(2, float(os.environ.get("CONTROL_REQUEST_TIMEOUT", "30"))),
            tls_verify=verify,
            software_version=os.environ.get("ALLFEEDS_WORKER_VERSION", "0.1.0"),
        )
