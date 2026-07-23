from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .settings import Settings

_pool: ConnectionPool | None = None
_lock = threading.Lock()


def validate_schema(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"invalid PostgreSQL schema: {value!r}")
    return value


def pool(settings: Settings | None = None) -> ConnectionPool:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                settings = settings or Settings.from_env()
                schema = validate_schema(settings.database_schema)
                _pool = ConnectionPool(
                    conninfo=settings.database_url,
                    min_size=settings.pool_min,
                    max_size=settings.pool_max,
                    timeout=settings.pool_timeout,
                    kwargs={
                        "row_factory": dict_row,
                        "options": f"-c search_path={schema},public",
                    },
                    open=True,
                )
    return _pool


@contextmanager
def connection(settings: Settings | None = None) -> Iterator[Connection]:
    with pool(settings).connection() as conn:
        yield conn


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def ensure_schema(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()
    with Connection.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                sql.Identifier(validate_schema(settings.database_schema))
            )
        )
