from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .app import Normalizer

TABLES = (
    "Ip",
    "Source",
    "Franchise",
    "Group",
    "Artist",
    "Venue",
    "Event",
    "TicketWindow",
    "ReleaseItem",
    "NewsPost",
    "EventArtist",
    "EventNews",
    "NewsIp",
)
FOREIGN_KEYS = {
    "Franchise": {"ipId": "Ip"},
    "Group": {"ipId": "Ip", "franchiseId": "Franchise"},
    "Artist": {"defaultGroupId": "Group"},
    "Event": {
        "venueId": "Venue",
        "ipId": "Ip",
        "franchiseId": "Franchise",
        "sourceId": "Source",
    },
    "TicketWindow": {"eventId": "Event"},
    "ReleaseItem": {"groupId": "Group", "ipId": "Ip"},
    "NewsPost": {"sourceId": "Source"},
    "EventArtist": {"eventId": "Event", "artistId": "Artist", "groupId": "Group"},
    "EventNews": {"eventId": "Event", "newsId": "NewsPost"},
    "NewsIp": {"newsId": "NewsPost", "ipId": "Ip"},
}
IP_SLUG_ALIASES = {
    "bangdream": "bang-dream",
    "gbc": "girls-band-cry",
    "lovelive": "love-live",
    "imas": "idolmaster",
}


def _validate_schema(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("legacy schema name is invalid")
    return value


def _target_columns(conn: psycopg.Connection, schema: str, table: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s
        """,
        (schema, table),
    ).fetchall()
    return {row["column_name"] for row in rows}


def _aware(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _lookup_id(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    row: dict[str, Any],
) -> str:
    if table == "Source":
        found = conn.execute(
            sql.SQL('SELECT "id" FROM {}.{} WHERE "id"=%s OR "key"=%s LIMIT 1').format(
                sql.Identifier(schema), sql.Identifier(table)
            ),
            (row["id"], row["key"]),
        ).fetchone()
    elif "slug" in row:
        found = conn.execute(
            sql.SQL('SELECT "id" FROM {}.{} WHERE "id"=%s OR "slug"=%s LIMIT 1').format(
                sql.Identifier(schema), sql.Identifier(table)
            ),
            (row["id"], row["slug"]),
        ).fetchone()
    else:
        found = conn.execute(
            sql.SQL('SELECT "id" FROM {}.{} WHERE "id"=%s LIMIT 1').format(
                sql.Identifier(schema), sql.Identifier(table)
            ),
            (row["id"],),
        ).fetchone()
    if not found:
        raise RuntimeError(f"could not resolve imported {table} row {row['id']}")
    return str(found["id"])


def import_legacy(
    normalizer: Normalizer,
    source_url: str,
    *,
    source_schema: str = "public",
    dry_run: bool = False,
) -> dict[str, int]:
    source_schema = _validate_schema(source_schema)
    target_schema = normalizer.settings.schema
    source = psycopg.connect(source_url, row_factory=dict_row)
    target = normalizer.connect()
    id_maps: dict[str, dict[str, str]] = {table: {} for table in TABLES}
    counts: dict[str, int] = {}
    try:
        for table in TABLES:
            source_rows = source.execute(
                sql.SQL("SELECT * FROM {}.{}").format(
                    sql.Identifier(source_schema), sql.Identifier(table)
                )
            ).fetchall()
            destination_columns = _target_columns(target, target_schema, table)
            imported = 0
            for source_row in source_rows:
                row = {key: _aware(value) for key, value in source_row.items()}
                old_id = str(row["id"]) if "id" in row else None

                if table == "Ip" and row.get("slug") in IP_SLUG_ALIASES:
                    target_slug = IP_SLUG_ALIASES[str(row["slug"])]
                    found = target.execute(
                        sql.SQL('SELECT "id" FROM {}.{} WHERE "slug"=%s').format(
                            sql.Identifier(target_schema), sql.Identifier(table)
                        ),
                        (target_slug,),
                    ).fetchone()
                    if not found:
                        raise RuntimeError(f"target IP is missing: {target_slug}")
                    id_maps[table][old_id] = str(found["id"])
                    continue

                if table == "Source":
                    row["key"] = f"legacy:{row['id']}"

                for column, referenced_table in FOREIGN_KEYS.get(table, {}).items():
                    value = row.get(column)
                    if value is not None:
                        mapped = id_maps[referenced_table].get(str(value))
                        if not mapped:
                            raise RuntimeError(
                                f"missing {referenced_table} mapping for {table}.{column}={value}"
                            )
                        row[column] = mapped

                columns = [column for column in row if column in destination_columns]
                statement = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
                    sql.Identifier(target_schema),
                    sql.Identifier(table),
                    sql.SQL(",").join(sql.Identifier(column) for column in columns),
                    sql.SQL(",").join(sql.Placeholder() for _ in columns),
                )
                cursor = target.execute(statement, [row[column] for column in columns])
                imported += max(0, cursor.rowcount)

                if old_id is not None:
                    id_maps[table][old_id] = _lookup_id(target, target_schema, table, row)
            counts[table] = imported

        if dry_run:
            target.rollback()
        else:
            target.commit()
        return counts
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()
