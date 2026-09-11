from __future__ import annotations

import hashlib
import hmac
import os
import smtplib
import ssl
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .emails import render_notification_email
from .localization import display_title, locale_of, translate
from .naming import change_summary
from .presentation import relevant
from .store import Catalog, uid
from .subscriptions import DIRECT_FOLLOW_MATCH


def signing_key() -> bytes:
    key = os.environ.get("PRODUCT_SECRET", "")
    if len(key) < 32:
        raise RuntimeError("PRODUCT_SECRET must contain at least 32 characters")
    return key.encode()


def unsubscribe_token(account_id: str) -> str:
    signature = hmac.new(
        signing_key(), f"unsubscribe:{account_id}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{account_id}.{signature}"


def verify_unsubscribe(token: str) -> str | None:
    account_id = token.partition(".")[0]
    return account_id if hmac.compare_digest(token, unsubscribe_token(account_id)) else None


def eligible_follows(
    conn, activity_id: str, account_id: str | None = None, *, for_delivery=True
) -> list[dict]:
    rows = conn.execute(
        f"""WITH RECURSIVE topics AS (
        SELECT s.slug,s.parent_slug FROM catalog_subjects s JOIN catalog_activity_subjects a ON a.subject_slug=s.slug WHERE a.activity_id=%s
        UNION SELECT s.slug,s.parent_slug FROM catalog_subjects s JOIN topics t ON t.parent_slug=s.slug
    ) SELECT f.*,u.email,u.timezone FROM genchi_private.follows f
    JOIN genchi_private.accounts u ON u.id=f.account_id
    JOIN catalog_activities a ON a.id=%s
    WHERE u.verified_at IS NOT NULL AND u.disabled_at IS NULL AND (NOT %s OR NOT u.unsubscribed) AND (%s::text IS NULL OR u.id=%s)
      AND a.publication='PUBLISHED' AND a.attendance IN ('OFFLINE','HYBRID')
      AND ({DIRECT_FOLLOW_MATCH} OR (f.target_type='SUBJECT'
        AND ((f.include_children AND f.target_id IN (SELECT slug FROM topics)) OR f.target_id IN
          (SELECT subject_slug FROM catalog_activity_subjects WHERE activity_id=a.id))))
      AND (jsonb_array_length(f.kinds)=0 OR f.kinds ? a.kind)
      AND (jsonb_array_length(f.cities)=0 OR EXISTS(SELECT 1 FROM catalog_occurrences o
        WHERE o.activity_id=a.id AND f.cities ? o.city)) ORDER BY
        CASE f.target_type WHEN 'ACTIVITY' THEN 0 WHEN 'SUBJECT' THEN 1 WHEN 'KEYWORD' THEN 2 ELSE 3 END,f.created_at DESC""",
        (activity_id, activity_id, for_delivery, account_id, account_id),
    ).fetchall()
    # Explicit activity preferences take priority over broader subject subscriptions.
    selected = {}
    for row in rows:
        selected.setdefault(row["account_id"], row)
    return list(selected.values())


def enqueue(
    conn,
    *,
    account_id,
    kind,
    dedup_key,
    due_at,
    payload=None,
    activity_id=None,
    milestone_id=None,
    revision=None,
):
    conn.execute(
        """INSERT INTO genchi_private.mail_queue(id,account_id,activity_id,milestone_id,milestone_revision,
        kind,dedup_key,due_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(dedup_key) DO UPDATE SET status='PENDING',due_at=EXCLUDED.due_at,
        payload=EXCLUDED.payload,lease_token=NULL WHERE mail_queue.status='CANCELED' """,
        (
            uid(),
            account_id,
            activity_id,
            milestone_id,
            revision,
            kind,
            dedup_key,
            due_at,
            Jsonb(payload or {}),
        ),
    )


def plan(catalog: Catalog, now: datetime | None = None):
    now = now or datetime.now(UTC)
    with catalog.connect() as conn, conn.transaction():
        # One planner at a time; delivery and collection are independent workers.
        if not conn.execute("SELECT pg_try_advisory_xact_lock(79321818) AS locked").fetchone()[
            "locked"
        ]:
            return
        activities = conn.execute(
            "SELECT id,status FROM catalog_activities WHERE publication='PUBLISHED'"
        ).fetchall()
        for activity in activities:
            follows = eligible_follows(conn, activity["id"])
            if not follows or activity["status"] in {"CANCELED", "POSTPONED", "ENDED"}:
                continue
            nodes = conn.execute(
                """SELECT m.* FROM catalog_milestones m WHERE m.activity_id=%s AND m.status='CONFIRMED'
                AND m.precision='TIME' AND COALESCE(m.ends_at,m.starts_at)>%s
                AND EXISTS(SELECT 1 FROM catalog_evidence e WHERE e.milestone_id=m.id AND e.verified)""",
                (activity["id"], now),
            ).fetchall()
            for follow in follows:
                for node in nodes:
                    rules = []
                    if node["kind"] in {"TICKET", "RESERVATION", "GOODS"}:
                        rules.append(("OPENING", node["starts_at"]))
                        if node["ends_at"] and follow["reminder_hours"]:
                            rules.append(
                                (
                                    "DEADLINE",
                                    max(
                                        now,
                                        node["ends_at"] - timedelta(hours=follow["reminder_hours"]),
                                    ),
                                )
                            )
                    elif node["kind"] == "RESULT":
                        rules.append(("RESULT", node["starts_at"]))
                    elif node["kind"] in {"PAYMENT", "START"} and follow["reminder_hours"]:
                        rules.append(
                            (
                                "DEADLINE" if node["kind"] == "PAYMENT" else "UPCOMING",
                                max(
                                    now,
                                    node["starts_at"] - timedelta(hours=follow["reminder_hours"]),
                                ),
                            )
                        )
                    for kind, due in rules:
                        if due < now:
                            continue
                        # Overlapping follows deliberately share the same key.
                        key = f"{follow['account_id']}:{node['id']}:{node['revision']}:{kind}"
                        enqueue(
                            conn,
                            account_id=follow["account_id"],
                            kind=kind,
                            dedup_key=key,
                            due_at=due,
                            activity_id=activity["id"],
                            milestone_id=node["id"],
                            revision=node["revision"],
                            payload={"lead_hours": follow["reminder_hours"]},
                        )
                        conn.execute(
                            "UPDATE genchi_private.mail_queue SET due_at=%s,payload=%s WHERE dedup_key=%s AND status='PENDING'",
                            (due, Jsonb({"lead_hours": follow["reminder_hours"]}), key),
                        )
        changes = conn.execute(
            "SELECT * FROM catalog_changes WHERE notify AND planned_at IS NULL ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 100"
        ).fetchall()
        for change in changes:
            for follow in eligible_follows(conn, change["activity_id"]):
                if change["kind"] in {"NEW_ACTIVITY", "PUBLISHED"}:
                    local = now.astimezone(ZoneInfo(follow["timezone"]))
                    due = datetime.combine(local.date(), time(9), ZoneInfo(follow["timezone"]))
                    if due <= local:
                        due += timedelta(days=1)
                    key = f"digest:{follow['account_id']}:{due.date()}"
                    enqueue(
                        conn,
                        account_id=follow["account_id"],
                        kind="DIGEST",
                        dedup_key=key,
                        due_at=due,
                        payload={"activity_ids": []},
                    )
                    conn.execute(
                        """UPDATE genchi_private.mail_queue SET payload=jsonb_set(payload,'{activity_ids}',
                        COALESCE(payload->'activity_ids','[]') || %s::jsonb) WHERE dedup_key=%s AND status='PENDING'
                        AND NOT (payload->'activity_ids' ? %s)""",
                        (Jsonb([change["activity_id"]]), key, change["activity_id"]),
                    )
                elif change["kind"] in {"NEW_MILESTONE", "MILESTONE_CHANGED", "STATUS_CHANGED"}:
                    if change["milestone_id"]:
                        node = conn.execute(
                            "SELECT * FROM catalog_milestones WHERE id=%s",
                            (change["milestone_id"],),
                        ).fetchone()
                        end_at = (node["ends_at"] or node["starts_at"]) if node else None
                        if end_at and end_at < now:
                            continue
                    # Bundle updates to one activity over five minutes; preserve every change ID.
                    window = int(now.timestamp()) // 300
                    key = f"updates:{follow['account_id']}:{change['activity_id']}:{window}"
                    due = datetime.fromtimestamp((window + 1) * 300, UTC)
                    enqueue(
                        conn,
                        account_id=follow["account_id"],
                        kind="UPDATE",
                        dedup_key=key,
                        due_at=due,
                        activity_id=change["activity_id"],
                        payload={"summary": "活动信息更新", "changes": []},
                    )
                    current = conn.execute(
                        "SELECT payload FROM genchi_private.mail_queue WHERE dedup_key=%s FOR UPDATE",
                        (key,),
                    ).fetchone()["payload"]
                    items = current.get("changes", [])
                    if not any(item["id"] == change["id"] for item in items):
                        items.append({"id": change["id"], "summary": change["summary"]})
                    current.update(
                        summary="活动信息更新",
                        changes=items,
                        before=change["before_value"],
                        after=change["after_value"],
                    )
                    conn.execute(
                        "UPDATE genchi_private.mail_queue SET payload=%s WHERE dedup_key=%s AND status='PENDING'",
                        (Jsonb(current), key),
                    )
            conn.execute(
                "UPDATE catalog_changes SET planned_at=%s WHERE id=%s", (now, change["id"])
            )


def format_time(value, timezone="Asia/Tokyo"):
    return value.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M") if value else "尚未公布"


def grouped_changes(changes: list[dict]) -> list[tuple[str, int]]:
    """Keep first-seen order while collapsing repeated normalization records."""
    counts = Counter(change_summary(item.get("summary", "活动信息更新")) for item in changes)
    return list(counts.items())


def localized_change(summary: str, count: int, locale: str) -> str:
    prefix, separator, item = summary.partition("：")
    if separator and prefix in {"新增", "更新"}:
        action = translate(prefix, locale)
        label = translate(item, locale)
        key = "{action}：{item}（{count} 项）" if count > 1 else "{action}：{item}"
        return translate(key, locale, action=action, item=label, count=count)
    label = translate(summary, locale)
    return (
        translate("{item}（{count} 项）", locale, item=label, count=count) if count > 1 else label
    )


def render_mail(conn, job, user, now):
    site = os.environ.get("PUBLIC_SITE_URL", "http://localhost:13000").rstrip("/")
    if job["kind"] == "LOGIN":
        # Link authentication is retired; OTP mail is sent only by the auth service.
        return None
    if user["unsubscribed"] or user.get("disabled_at"):
        return None
    locale = locale_of(user.get("locale"))

    def t(key, **values):
        return translate(key, locale, **values)

    unsubscribe_url = f"{site}/{locale}/unsubscribe?token={unsubscribe_token(user['id'])}"
    if job["kind"] == "DIGEST":
        items = []
        for activity_id in job["payload"].get("activity_ids", []):
            if eligible_follows(conn, activity_id, user["id"]):
                activity = conn.execute(
                    "SELECT title,title_zh FROM catalog_activities WHERE id=%s", (activity_id,)
                ).fetchone()
                items.append(
                    (
                        display_title(activity, locale),
                        f"{site}/{locale}/activities/{activity_id}",
                    )
                )
        if not items:
            return None
        rendered = render_notification_email(
            t("你关注的新活动 · Genchi"),
            locale=locale,
            site_url=site,
            eyebrow=t("新活动汇总"),
            heading=t("你关注的新活动 · Genchi"),
            items=items,
            cta_label=t("查看我的日程"),
            cta_url=f"{site}/{locale}/following?tab=activities",
            unsubscribe_url=unsubscribe_url,
        )
        return rendered.subject, rendered.text, rendered.html
    follows = eligible_follows(conn, job["activity_id"], user["id"])
    if not follows:
        return None
    if job["kind"] in {"DEADLINE", "UPCOMING"} and not follows[0]["reminder_hours"]:
        return None
    activity = conn.execute(
        "SELECT * FROM catalog_activities WHERE id=%s FOR UPDATE", (job["activity_id"],)
    ).fetchone()
    link = f"{site}/{locale}/activities/{activity['id']}"
    if job["kind"] == "UPDATE":
        status_label = {"CANCELED": t("已取消"), "POSTPONED": t("已延期")}.get(
            activity["status"], ""
        )
        changes = job["payload"].get("changes", [])
        groups = grouped_changes(changes)
        items = [(localized_change(summary, count, locale), None) for summary, count in groups[:8]]
        if len(groups) > 8:
            items.append(
                (t("另有 {count} 类更新，请在活动详情中查看。", count=len(groups) - 8), None)
            )
        facts = [(t("最新状态"), status_label)] if status_label else []
        after = job["payload"].get("after")
        # A bundled payload retains only the final raw before/after pair. Showing it
        # beside many changes would imply that it describes all of them.
        if len(changes) == 1 and isinstance(after, dict):
            for key, label in (
                ("starts_at", t("更新后的开始时间")),
                ("ends_at", t("更新后的截止时间")),
            ):
                if after.get(key):
                    facts.append((label, f"{format_time(datetime.fromisoformat(after[key]))} JST"))
        subject = f"{t('活动信息更新')}{('（' + status_label + '）') if status_label else ''} · {display_title(activity, locale)}"
        rendered = render_notification_email(
            subject,
            locale=locale,
            site_url=site,
            eyebrow=t("活动信息更新"),
            heading=display_title(activity, locale),
            intro=t(
                "这次共更新 {count} 项记录，已为你合并相同内容。",
                count=len(changes),
            ),
            items=items,
            facts=facts,
            cta_label=t("活动详情与官方依据"),
            cta_url=link,
            unsubscribe_url=unsubscribe_url,
        )
        return rendered.subject, rendered.text, rendered.html
    node = conn.execute(
        "SELECT * FROM catalog_milestones WHERE id=%s FOR UPDATE", (job["milestone_id"],)
    ).fetchone()
    if (
        not node
        or node["revision"] != job["milestone_revision"]
        or node["status"] != "CONFIRMED"
        or activity["status"] in {"CANCELED", "POSTPONED", "ENDED"}
    ):
        return None
    deadline = node["ends_at"] or node["starts_at"]
    if deadline and deadline < now - timedelta(minutes=10):
        return None
    if node["requires"] != "NONE":
        states = conn.execute(
            "SELECT * FROM genchi_private.participation WHERE account_id=%s AND activity_id=%s",
            (user["id"], activity["id"]),
        ).fetchall()
        if node["requires"] == "WON" and not any(
            s["status"] == "WON" and s["round_key"] == node["round_key"] for s in states
        ):
            return None
        if node["requires"] == "APPLIED" and not any(
            s["status"] in {"APPLIED", "WON", "PURCHASED"} and s["round_key"] == node["round_key"]
            for s in states
        ):
            return None
    progress = conn.execute(
        "SELECT status FROM genchi_private.participation WHERE account_id=%s AND activity_id=%s AND round_key=%s",
        (user["id"], activity["id"], node["round_key"]),
    ).fetchone()
    if not relevant(node, progress["status"] if progress else None):
        return None
    labels = {
        "OPENING": t("开始申请"),
        "DEADLINE": t("截止提醒"),
        "RESULT": t("结果公布"),
        "UPCOMING": t("活动提醒"),
    }
    heading = f"{labels.get(job['kind'], t('活动提醒'))} · {display_title(node, locale)}"
    facts = [(t("开始"), f"{format_time(node['starts_at'])} JST")]
    if node["ends_at"]:
        facts.append((t("截止"), f"{format_time(node['ends_at'])} JST"))
    facts.append(
        (
            t("你的时区"),
            f"{format_time(deadline, user['timezone'])} ({user['timezone']})",
        )
    )
    if node["eligibility"]:
        facts.append((t("适用条件"), node["eligibility"]))
    items = []
    if node["kind"] == "RESULT":
        items.append((t("本邮件仅提示结果发表，请自行前往官方平台确认是否中选。"), None))
    rendered = render_notification_email(
        heading,
        locale=locale,
        site_url=site,
        eyebrow=labels.get(job["kind"], t("活动提醒")),
        heading=display_title(activity, locale),
        intro=display_title(node, locale),
        items=items,
        facts=facts,
        cta_label=t("活动详情与官方依据"),
        cta_url=link,
        official_url=node["url"],
        unsubscribe_url=unsubscribe_url,
    )
    return rendered.subject, rendered.text, rendered.html


def deliver_one(catalog: Catalog, *, now: datetime | None = None, transport=None) -> bool:
    now = now or datetime.now(UTC)
    lease = uid()
    with catalog.connect() as conn, conn.transaction():
        # A crashed delivery may have been accepted by SMTP. Do not blindly resend it.
        conn.execute(
            "UPDATE genchi_private.mail_queue SET status='UNCERTAIN',last_error='投递进程中断，需核对邮件服务回执' WHERE status='SENDING' AND locked_at<NOW()-INTERVAL '5 minutes'"
        )
        job = conn.execute(
            """WITH next AS (SELECT id FROM genchi_private.mail_queue WHERE status='PENDING' AND due_at<=%s
            ORDER BY due_at FOR UPDATE SKIP LOCKED LIMIT 1) UPDATE genchi_private.mail_queue q SET
            status='SENDING',lease_token=%s,locked_at=NOW(),attempts=attempts+1 FROM next WHERE q.id=next.id RETURNING q.*""",
            (now, lease),
        ).fetchone()
    if not job:
        return False
    try:
        with catalog.connect() as conn, conn.transaction():
            # Serialize with publication and opt-out, then recheck the effective version immediately before sending.
            if job["activity_id"]:
                conn.execute(
                    "SELECT id FROM catalog_activities WHERE id=%s FOR UPDATE",
                    (job["activity_id"],),
                )
            if job["milestone_id"]:
                conn.execute(
                    "SELECT id FROM catalog_milestones WHERE id=%s FOR UPDATE",
                    (job["milestone_id"],),
                )
            user = conn.execute(
                "SELECT * FROM genchi_private.accounts WHERE id=%s FOR UPDATE", (job["account_id"],)
            ).fetchone()
            current = conn.execute(
                "SELECT * FROM genchi_private.mail_queue WHERE id=%s FOR UPDATE", (job["id"],)
            ).fetchone()
            if current["status"] != "SENDING" or current["lease_token"] != lease:
                return True
            if job["kind"] in {"DEADLINE", "UPCOMING"}:
                follows = eligible_follows(conn, job["activity_id"], user["id"])
                node = conn.execute(
                    "SELECT * FROM catalog_milestones WHERE id=%s", (job["milestone_id"],)
                ).fetchone()
                if follows and node and node["revision"] == job["milestone_revision"]:
                    anchor = node["ends_at"] or node["starts_at"]
                    hours = follows[0]["reminder_hours"]
                    if hours and anchor and anchor - timedelta(hours=hours) > now:
                        conn.execute(
                            "UPDATE genchi_private.mail_queue SET status='PENDING',due_at=%s,lease_token=NULL WHERE id=%s",
                            (anchor - timedelta(hours=hours), job["id"]),
                        )
                        return True
            content = render_mail(conn, job, user, now)
            if content is None:
                conn.execute(
                    "UPDATE genchi_private.mail_queue SET status='CANCELED',lease_token=NULL WHERE id=%s",
                    (job["id"],),
                )
                return True
            if transport:
                transport(user["email"], content[0], content[1], job["id"], user["id"])
            else:
                smtp_send(
                    user["email"],
                    content[0],
                    content[1],
                    job["id"],
                    user["id"],
                    html=content[2],
                )
            conn.execute(
                "UPDATE genchi_private.mail_queue SET status='SENT',sent_at=NOW(),lease_token=NULL,last_error=NULL WHERE id=%s",
                (job["id"],),
            )
    except Exception as exc:
        with catalog.connect() as conn:
            conn.execute(
                "UPDATE genchi_private.mail_queue SET status='UNCERTAIN',lease_token=NULL,last_error=%s WHERE id=%s AND lease_token=%s",
                (f"{type(exc).__name__}：请核对邮件服务后重试，避免重复投递", job["id"], lease),
            )
    return True


def smtp_send(recipient, subject, body, message_id, account_id, *, timeout=20, html=None):
    host = os.environ.get("SMTP_HOST", "mailpit")
    security = os.environ.get("SMTP_SECURITY", "none")
    if security not in {"none", "ssl", "starttls"}:
        raise RuntimeError("Unsupported SMTP_SECURITY")
    if security == "none" and host not in {"mailpit", "localhost", "127.0.0.1"}:
        raise RuntimeError("Remote SMTP requires TLS")
    message = EmailMessage()
    message["From"] = os.environ.get("MAIL_FROM", "Genchi <hello@genchi.local>")
    message["To"] = recipient
    message["Subject"] = subject
    message["Message-ID"] = f"<{message_id}@genchi.local>"
    if host.lower() == "smtp.resend.com":
        message["Resend-Idempotency-Key"] = message_id
    if account_id is not None:
        site = os.getenv("PUBLIC_SITE_URL", "http://localhost:13000")
        message["List-Unsubscribe"] = (
            f"<{site}/api/product/auth/unsubscribe?token={unsubscribe_token(account_id)}>"
        )
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message.set_content(body)
    if html is not None:
        message.add_alternative(html, subtype="html")
    client = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    options = {"timeout": timeout}
    if security == "ssl":
        options["context"] = ssl.create_default_context()
    with client(host, int(os.getenv("SMTP_PORT", "1025")), **options) as smtp:
        if security == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        if os.getenv("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        if smtp.send_message(message):
            raise RuntimeError("SMTP did not accept the recipient")
