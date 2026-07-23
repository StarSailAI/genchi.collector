from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import psycopg
from allfeeds_contracts import ResourceAsset, ResourceRecord
from allfeeds_sdk import SinkPlugin
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def _schema(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"invalid database schema: {value!r}")
    return value


def _content_hash(record: ResourceRecord) -> str:
    payload = json.dumps(
        {
            "kind": record.kind,
            "url": record.url,
            "title": record.title,
            "content": record.content,
            "content_type": record.content_type,
            "language": record.language,
            "published_at": record.published_at.isoformat() if record.published_at else None,
            "attributes": record.attributes,
            "tags": record.tags,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class PostgresSink(SinkPlugin):
    name = "postgres"

    def __init__(self, database_url: str | None = None, schema: str | None = None):
        import os

        self.database_url = database_url or os.environ.get("DATABASE_URL", "")
        self.schema = _schema(schema or os.environ.get("ALLFEEDS_DB_SCHEMA", "allfeeds"))
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required")

    def _connect(self):
        return psycopg.connect(
            self.database_url,
            row_factory=dict_row,
            options=f"-c search_path={self.schema},public",
        )

    def write_records(self, source_id: str, records: list[ResourceRecord]) -> dict[str, int]:
        counts = {"added": 0, "updated": 0, "duplicate": 0}
        if not records:
            return counts
        with self._connect() as conn, conn.transaction():
            for record in records:
                digest = _content_hash(record)
                current = conn.execute(
                    "SELECT id,content_hash FROM resources WHERE source_id=%s AND external_id=%s FOR UPDATE",
                    (source_id, record.external_id),
                ).fetchone()
                values = (
                    record.kind,
                    record.url,
                    record.title,
                    record.content,
                    record.content_type,
                    record.language,
                    record.published_at,
                    record.observed_at,
                    digest,
                    Jsonb(record.attributes),
                    Jsonb(list(record.tags)),
                )
                if current is None:
                    resource = conn.execute(
                        """
                        INSERT INTO resources (
                            source_id,external_id,kind,url,title,content,content_type,language,
                            published_at,observed_at,content_hash,attributes,tags
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,NOW()),%s,%s,%s)
                        RETURNING id
                        """,
                        (source_id, record.external_id, *values),
                    ).fetchone()
                    counts["added"] += 1
                    resource_id = resource["id"]
                elif current["content_hash"] != digest:
                    conn.execute(
                        """
                        UPDATE resources SET kind=%s,url=%s,title=%s,content=%s,content_type=%s,
                            language=%s,published_at=%s,observed_at=COALESCE(%s,NOW()),
                            content_hash=%s,attributes=%s,tags=%s,updated_at=NOW()
                        WHERE id=%s
                        """,
                        (*values, current["id"]),
                    )
                    counts["updated"] += 1
                    resource_id = current["id"]
                else:
                    counts["duplicate"] += 1
                    conn.execute(
                        """
                        UPDATE resources SET observed_at=COALESCE(%s,NOW()),updated_at=NOW()
                        WHERE id=%s
                        """,
                        (record.observed_at, current["id"]),
                    )
                    continue
                conn.execute(
                    """
                    INSERT INTO resource_versions
                        (resource_id,content_hash,title,content,attributes,observed_at)
                    VALUES (%s,%s,%s,%s,%s,COALESCE(%s,NOW()))
                    ON CONFLICT (resource_id,content_hash) DO NOTHING
                    """,
                    (
                        resource_id,
                        digest,
                        record.title,
                        record.content,
                        Jsonb(record.attributes),
                        record.observed_at,
                    ),
                )
        return counts

    def write_assets(self, source_id: str, assets: list[ResourceAsset]) -> int:
        if not assets:
            return 0
        with self._connect() as conn, conn.transaction():
            for asset in assets:
                conn.execute(
                    """
                    INSERT INTO resource_assets (
                        source_id,external_id,asset_key,url,media_type,size_bytes,
                        checksum,storage_uri,metadata
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (source_id,external_id,asset_key) DO UPDATE SET
                        url=EXCLUDED.url,media_type=EXCLUDED.media_type,
                        size_bytes=EXCLUDED.size_bytes,checksum=EXCLUDED.checksum,
                        storage_uri=EXCLUDED.storage_uri,metadata=EXCLUDED.metadata,updated_at=NOW()
                    """,
                    (
                        source_id,
                        asset.external_id,
                        asset.asset_key,
                        asset.url,
                        asset.media_type,
                        asset.size_bytes,
                        asset.checksum,
                        asset.storage_uri,
                        Jsonb(asset.metadata),
                    ),
                )
        return len(assets)

    def load_checkpoint(self, source_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT checkpoint,etag,last_modified FROM fetch_states WHERE source_id=%s",
                (source_id,),
            ).fetchone()
        if not row:
            return {}
        return {
            **dict(row["checkpoint"] or {}),
            "etag": row["etag"],
            "last_modified": row["last_modified"],
        }

    def save_checkpoint(self, source_id: str, value: dict[str, Any]) -> None:
        value = dict(value)
        etag = value.pop("etag", None)
        last_modified = value.pop("last_modified", None)
        with self._connect() as conn, conn.transaction():
            conn.execute(
                """
                INSERT INTO fetch_states (source_id,checkpoint,etag,last_modified)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (source_id) DO UPDATE SET checkpoint=EXCLUDED.checkpoint,
                    etag=COALESCE(EXCLUDED.etag,fetch_states.etag),
                    last_modified=COALESCE(EXCLUDED.last_modified,fetch_states.last_modified),
                    updated_at=NOW()
                """,
                (source_id, Jsonb(value), etag, last_modified),
            )
