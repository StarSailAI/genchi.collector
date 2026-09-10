"""Daily admin summary for newly stored collection resource versions."""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, time, timedelta
from email.utils import getaddresses, parseaddr
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .inbound import InboundError, Resend
from .store import Catalog

LOG = logging.getLogger(__name__)
MAX_ITEMS = 100


def _clock(name: str, default: str) -> time:
    raw = os.getenv(name, default).strip()
    try:
        return time.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must use HH:MM") from exc


def _recipient() -> str:
    raw = os.getenv("ADMIN_EMAIL", "").strip()
    addresses = getaddresses([raw])
    if len(addresses) != 1 or addresses[0][1] != raw or parseaddr(raw)[1] != raw:
        raise ValueError("ADMIN_EMAIL must contain exactly one email address")
    return raw


def _target_date(now: datetime, zone: ZoneInfo, not_before: time) -> date:
    local = now.astimezone(zone)
    return local.date() if local.time() >= not_before else local.date() - timedelta(days=1)


def _window(target: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(target, time.min, zone).astimezone(UTC)
    return start, (datetime.combine(target + timedelta(days=1), time.min, zone)).astimezone(UTC)


def render_digest(target: date, rows: list[dict], total: int, source_counts: dict[str, int]):
    subject = f"Genchi 每日采集：新增或更新 {total} 条（{target.isoformat()}）"
    lines = [
        "Genchi 当日爬取后的数据入库汇总",
        "",
        f"日本日期：{target.isoformat()}",
        f"新增或更新版本：{total} 条",
        "",
        "来源统计：",
    ]
    lines.extend(f"- {source}: {count} 条" for source, count in sorted(source_counts.items()))
    lines.extend(["", "数据明细："])
    for row in rows:
        label = "新增" if row["is_new"] else "更新"
        title = " ".join((row.get("title") or "（无标题）").split())[:300]
        lines.append(f"- [{label}] {title}")
        lines.append(f"  来源：{row['source_id']}")
        if row.get("url"):
            lines.append(f"  {row['url']}")
    if total > len(rows):
        lines.extend(["", f"另有 {total - len(rows)} 条未在邮件中展开，可在管理端查看。"])
    return subject, "\n".join(lines)


def prepare_due_digest(catalog: Catalog, now: datetime | None = None) -> str:
    """Freeze one due digest after scheduled crawls, or record an empty day."""
    now = now or datetime.now(UTC)
    zone = ZoneInfo(os.getenv("COLLECTION_DIGEST_TIMEZONE", "Asia/Tokyo"))
    not_before = _clock("COLLECTION_DIGEST_NOT_BEFORE", "18:00")
    force_at = _clock("COLLECTION_DIGEST_FORCE_AT", "23:00")
    if force_at <= not_before:
        raise ValueError("COLLECTION_DIGEST_FORCE_AT must be after COLLECTION_DIGEST_NOT_BEFORE")
    target = _target_date(now, zone, not_before)
    day_start, day_end = _window(target, zone)
    local = now.astimezone(zone)
    forced = local.date() > target or local.time() >= force_at
    with catalog.connect() as conn, conn.transaction():
        if not conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(current_schema() || ':collection-digest',0)) locked"
        ).fetchone()["locked"]:
            return "busy"
        existing = conn.execute(
            "SELECT status FROM genchi_private.collection_digests WHERE digest_date=%s",
            (target,),
        ).fetchone()
        if existing:
            return existing["status"].lower()
        unresolved = conn.execute(
            """SELECT status FROM genchi_private.collection_digests
            WHERE status NOT IN ('SENT','EMPTY') ORDER BY digest_date LIMIT 1"""
        ).fetchone()
        if unresolved:
            return "awaiting_" + unresolved["status"].lower()
        if not forced:
            active = conn.execute(
                """SELECT count(*) count FROM allfeeds.tasks WHERE workload='scheduled'
                AND scheduled_for>=%s AND scheduled_for<%s
                AND status IN ('pending','retry','running')""",
                (day_start, day_end),
            ).fetchone()["count"]
            if active:
                return "waiting_for_collection"
        state = conn.execute(
            "SELECT last_version_id FROM genchi_private.collection_digest_state WHERE id=TRUE FOR UPDATE"
        ).fetchone()
        if not state:
            raise RuntimeError("Collection digest cursor is missing")
        start_id = state["last_version_id"]
        end_id = conn.execute(
            "SELECT COALESCE(max(id),%s) end_id FROM allfeeds.resource_versions", (start_id,)
        ).fetchone()["end_id"]
        if end_id == start_id:
            conn.execute(
                """INSERT INTO genchi_private.collection_digests
                (digest_date,window_start_id,window_end_id,status,counts)
                VALUES(%s,%s,%s,'EMPTY','{}')""",
                (target, start_id, end_id),
            )
            return "empty"
        rows = conn.execute(
            """SELECT v.id,r.source_id,r.title,r.url,
            NOT EXISTS(SELECT 1 FROM allfeeds.resource_versions older
              WHERE older.resource_id=v.resource_id AND older.id<v.id) is_new
            FROM allfeeds.resource_versions v JOIN allfeeds.resources r ON r.id=v.resource_id
            WHERE v.id>%s AND v.id<=%s ORDER BY v.id LIMIT %s""",
            (start_id, end_id, MAX_ITEMS),
        ).fetchall()
        counts = conn.execute(
            """SELECT r.source_id,count(*) count FROM allfeeds.resource_versions v
            JOIN allfeeds.resources r ON r.id=v.resource_id WHERE v.id>%s AND v.id<=%s
            GROUP BY r.source_id ORDER BY r.source_id""",
            (start_id, end_id),
        ).fetchall()
        total = sum(row["count"] for row in counts)
        source_counts = {row["source_id"]: row["count"] for row in counts}
        subject, body = render_digest(target, rows, total, source_counts)
        payload = {
            "from": os.environ["MAIL_FROM"],
            "to": [_recipient()],
            "subject": subject,
            "text": body,
        }
        conn.execute(
            """INSERT INTO genchi_private.collection_digests
            (digest_date,window_start_id,window_end_id,status,counts,payload)
            VALUES(%s,%s,%s,'PENDING',%s,%s)""",
            (
                target,
                start_id,
                end_id,
                Jsonb({"total": total, "sources": source_counts}),
                Jsonb(payload),
            ),
        )
        return "pending"


def deliver_digest(
    catalog: Catalog, client: Resend | None = None, now: datetime | None = None
) -> bool:
    """Deliver one frozen digest with Resend's 24-hour idempotency boundary."""
    now = now or datetime.now(UTC)
    with catalog.connect() as conn, conn.transaction():
        conn.execute(
            """UPDATE genchi_private.collection_digests SET status='PENDING',locked_at=NULL,
            available_at=NOW() WHERE status='SENDING' AND locked_at<NOW()-INTERVAL '5 minutes'
            AND first_send_at>NOW()-INTERVAL '23 hours'"""
        )
        row = conn.execute(
            """SELECT * FROM genchi_private.collection_digests WHERE status='PENDING'
            AND available_at<=%s ORDER BY digest_date FOR UPDATE SKIP LOCKED LIMIT 1""",
            (now,),
        ).fetchone()
        if not row:
            return False
        if row["first_send_at"] and row["first_send_at"] < now - timedelta(hours=23):
            conn.execute(
                """UPDATE genchi_private.collection_digests SET status='UNCERTAIN',
                last_error='idempotency_window_expired' WHERE digest_date=%s""",
                (row["digest_date"],),
            )
            return True
        row = conn.execute(
            """UPDATE genchi_private.collection_digests SET status='SENDING',locked_at=NOW(),
            first_send_at=COALESCE(first_send_at,NOW()),attempts=attempts+1
            WHERE digest_date=%s RETURNING *""",
            (row["digest_date"],),
        ).fetchone()
    try:
        result = (client or Resend()).request(
            "POST",
            "/emails",
            payload=row["payload"],
            idempotency_key=f"genchi-collection-digest/{row['digest_date'].isoformat()}",
        )
        resend_id = str(UUID(result["id"]))
        with catalog.connect() as conn, conn.transaction():
            current = conn.execute(
                "SELECT status FROM genchi_private.collection_digests WHERE digest_date=%s FOR UPDATE",
                (row["digest_date"],),
            ).fetchone()
            if not current or current["status"] != "SENDING":
                return True
            conn.execute(
                """UPDATE genchi_private.collection_digests SET status='SENT',sent_at=NOW(),
                resend_email_id=%s,payload=NULL,locked_at=NULL,last_error=NULL WHERE digest_date=%s""",
                (resend_id, row["digest_date"]),
            )
            conn.execute(
                """UPDATE genchi_private.collection_digest_state SET last_version_id=GREATEST(last_version_id,%s),
                updated_at=NOW() WHERE id=TRUE""",
                (row["window_end_id"],),
            )
        LOG.info("Collection digest %s sent as %s", row["digest_date"], resend_id)
    except (InboundError, KeyError, ValueError) as exc:
        permanent = isinstance(exc, InboundError) and exc.permanent
        code = str(exc) if isinstance(exc, InboundError) else "invalid_resend_response"
        delay = min(3600, 30 * 2 ** min(row["attempts"], 7))
        with catalog.connect() as conn:
            conn.execute(
                """UPDATE genchi_private.collection_digests SET status=%s,last_error=%s,
                available_at=NOW()+(%s * INTERVAL '1 second'),locked_at=NULL WHERE digest_date=%s""",
                ("FAILED" if permanent else "PENDING", code, delay, row["digest_date"]),
            )
        LOG.warning("Collection digest %s: %s", row["digest_date"], code)
    return True
