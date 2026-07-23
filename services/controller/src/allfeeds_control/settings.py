from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    database_schema: str
    config_path: Path
    control_token: str
    allow_empty_token: bool
    registrar_tick_seconds: float
    node_offline_seconds: int
    task_stale_seconds: int
    resident_backfill_slots: int
    pool_min: int
    pool_max: int
    pool_timeout: float

    @classmethod
    def from_env(cls) -> Settings:
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        token = os.environ.get("CONTROL_API_TOKEN", "").strip()
        allow_empty = _bool("ALLOW_EMPTY_CONTROL_TOKEN")
        if not token and not allow_empty:
            raise RuntimeError(
                "CONTROL_API_TOKEN is required; ALLOW_EMPTY_CONTROL_TOKEN is for local use only"
            )
        return cls(
            database_url=database_url,
            database_schema=os.environ.get("ALLFEEDS_DB_SCHEMA", "allfeeds").strip(),
            config_path=Path(os.environ.get("ALLFEEDS_SOURCES", "/app/config/sources.yaml")),
            control_token=token,
            allow_empty_token=allow_empty,
            registrar_tick_seconds=max(1.0, float(os.environ.get("REGISTRAR_TICK_SECONDS", "10"))),
            node_offline_seconds=max(15, int(os.environ.get("NODE_OFFLINE_SECONDS", "60"))),
            task_stale_seconds=max(30, int(os.environ.get("TASK_STALE_SECONDS", "180"))),
            resident_backfill_slots=max(0, int(os.environ.get("RESIDENT_BACKFILL_SLOTS", "1"))),
            pool_min=max(1, int(os.environ.get("DB_POOL_MIN", "1"))),
            pool_max=max(2, int(os.environ.get("DB_POOL_MAX", "32"))),
            pool_timeout=max(0.5, float(os.environ.get("DB_POOL_TIMEOUT", "5"))),
        )
