from __future__ import annotations

import hashlib
import os
import secrets
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool
from svix.webhooks import Webhook, WebhookVerificationError

from .domain import KINDS, ActivityInput
from .inbound import enqueue_received
from .naming import change_summary, sync_name
from .notifications import enqueue, signing_key, verify_unsubscribe
from .presentation import agenda_groups, followed_ids
from .store import Catalog, uid

# DATE anchors are only for sorting; the public precision and calendar values remain DATE.
NEXT_ACTION_AT = """(CASE WHEN COALESCE(m.starts_at,m.starts_on::timestamp AT TIME ZONE 'Asia/Tokyo')>NOW()
  THEN COALESCE(m.starts_at,m.starts_on::timestamp AT TIME ZONE 'Asia/Tokyo')
  ELSE COALESCE(m.ends_at,(m.ends_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo',m.starts_at,(m.starts_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo') END)"""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def account(request: Request, conn):
    token = request.cookies.get("genchi_session", "")
    user = conn.execute(
        """SELECT a.* FROM genchi_private.sessions s JOIN genchi_private.accounts a ON a.id=s.account_id
        WHERE s.token_hash=%s AND s.expires_at>NOW() AND a.verified_at IS NOT NULL""",
        (digest(token),),
    ).fetchone()
    if not user:
        raise HTTPException(401, "请先验证邮箱并登录")
    return user


def admin(request: Request, conn):
    configured = os.environ.get("PRODUCT_ADMIN_TOKEN", "")
    if configured and secrets.compare_digest(request.headers.get("X-Admin-Token", ""), configured):
        return "admin-token"
    user = account(request, conn)
    if user["email"] != os.environ.get("ADMIN_EMAIL", ""):
        raise HTTPException(403, "需要管理员权限")
    return user["id"]


def public_activity(conn, activity_id):
    alias = conn.execute(
        "SELECT activity_id FROM catalog_external_ids WHERE key=%s", ("redirect:" + activity_id,)
    ).fetchone()
    if alias:
        activity_id = alias["activity_id"]
    row = conn.execute(
        "SELECT * FROM catalog_activities WHERE id=%s AND publication='PUBLISHED' AND attendance IN ('OFFLINE','HYBRID')",
        (activity_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "没有找到此活动")
    return row


def hydrate(conn, rows):
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    subjects = conn.execute(
        """SELECT l.activity_id,s.* FROM catalog_activity_subjects l
        JOIN catalog_subjects s ON s.slug=l.subject_slug WHERE l.activity_id=ANY(%s)""",
        (ids,),
    ).fetchall()
    occurrences = conn.execute(
        "SELECT * FROM catalog_occurrences WHERE activity_id=ANY(%s) ORDER BY COALESCE(starts_at,starts_on::timestamptz) NULLS LAST",
        (ids,),
    ).fetchall()
    nodes = conn.execute(
        """SELECT m.*,
        EXISTS(SELECT 1 FROM catalog_evidence e WHERE e.milestone_id=m.id AND e.verified) AS verified,
        COALESCE((SELECT jsonb_agg(occurrence_id) FROM catalog_milestone_scopes s WHERE s.milestone_id=m.id),'[]') AS occurrence_ids,
        CASE WHEN COALESCE(m.starts_at,m.starts_on::timestamp AT TIME ZONE 'Asia/Tokyo')>NOW()
          THEN COALESCE(m.starts_at,m.starts_on::timestamp AT TIME ZONE 'Asia/Tokyo')
          ELSE COALESCE(m.ends_at,(m.ends_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo',m.starts_at,(m.starts_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo') END AS action_at
        FROM catalog_milestones m WHERE activity_id=ANY(%s) AND status='CONFIRMED'
        AND COALESCE(m.ends_at,(m.ends_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo',m.starts_at,(m.starts_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo')>NOW()
        ORDER BY action_at,m.id""",
        (ids,),
    ).fetchall()
    counts = conn.execute(
        "SELECT activity_id,count(DISTINCT url) AS source_count,max(observed_at) AS checked_at FROM catalog_evidence WHERE activity_id=ANY(%s) GROUP BY activity_id",
        (ids,),
    ).fetchall()
    node_counts = {
        r["activity_id"]: r["count"]
        for r in conn.execute(
            "SELECT activity_id,count(*) FROM catalog_milestones WHERE activity_id=ANY(%s) AND status<>'SUPERSEDED' GROUP BY activity_id",
            (ids,),
        ).fetchall()
    }
    for row in rows:
        row["subjects"] = [s for s in subjects if s["activity_id"] == row["id"]]
        row["occurrences"] = [s for s in occurrences if s["activity_id"] == row["id"]]
        row["next_milestone"] = next((n for n in nodes if n["activity_id"] == row["id"]), None)
        row["milestone_count"] = node_counts.get(row["id"], 0)
        row.update(
            next(
                (c for c in counts if c["activity_id"] == row["id"]),
                {"source_count": 0, "checked_at": None},
            )
        )
    return rows


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=5, max_length=254, pattern=r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
    timezone: str = "Asia/Shanghai"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError("Invalid timezone") from exc
        return value


class TokenBody(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class FollowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_type: str = Field(pattern="^(SUBJECT|ACTIVITY)$")
    target_id: str = Field(min_length=1, max_length=200)
    reminder_hours: int = 24
    include_children: bool = True
    kinds: list[str] = Field(default_factory=list, max_length=8)
    cities: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("reminder_hours")
    @classmethod
    def hours(cls, value):
        if value not in {0, 2, 24, 48}:
            raise ValueError("Invalid reminder time")
        return value

    @field_validator("kinds")
    @classmethod
    def kinds_known(cls, value):
        if set(value) - KINDS:
            raise ValueError("Invalid activity kind")
        return value


class ParticipationBody(BaseModel):
    status: str = Field(pattern="^(INTERESTED|APPLIED|WON|PURCHASED)$")
    round_key: str = Field(default="", max_length=2000)


class ReviewBody(BaseModel):
    approve: bool
    activity: ActivityInput | None = None


class NameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: str = Field(pattern="^(ACTIVITY|MILESTONE|OCCURRENCE|SUBJECT)$")
    entity_id: str = Field(min_length=1, max_length=200)
    source_text: str = Field(min_length=1, max_length=4000)
    display_text: str = Field(min_length=1, max_length=1000)


def create_app(catalog: Catalog | None = None):
    catalog = catalog or Catalog()
    app = FastAPI(title="Genchi Product", version="0.2.0")

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            expected = os.getenv("PUBLIC_SITE_URL", "http://localhost:13000").rstrip("/")
            if origin and origin.rstrip("/") != expected:
                return Response("Origin not allowed", status_code=403)
        response = await call_next(request)
        if request.url.path.startswith(("/auth", "/me", "/admin")):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health")
    def health():
        with catalog.connect() as conn:
            contract = conn.execute(
                'SELECT major,minor FROM "SchemaContract" WHERE id=1'
            ).fetchone()
            ready = bool(contract and contract["major"] == 1 and contract["minor"] >= 4)
            if not ready:
                raise HTTPException(503, "Catalog schema 1.4 required")
            return {
                "ok": True,
                "schema": f"{contract['major']}.{contract['minor']}",
                "service": "genchi-product",
            }

    @app.post("/webhooks/resend")
    async def resend_received(request: Request):
        secret = os.environ.get("RESEND_WEBHOOK_SECRET", "")
        if not secret:
            raise HTTPException(503, "Resend inbound is not configured")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 65536:
                raise HTTPException(413, "Webhook payload too large")
            raw.extend(chunk)
        try:
            event = Webhook(secret).verify(bytes(raw), dict(request.headers))
        except WebhookVerificationError:
            raise HTTPException(401, "Invalid webhook signature") from None
        except ValueError:
            raise HTTPException(400, "Invalid webhook payload") from None
        if not isinstance(event, dict):
            raise HTTPException(400, "Invalid webhook payload")
        if event.get("type") != "email.received":
            return {"ok": True, "ignored": True}
        try:
            email_id = event["data"]["email_id"]
            if not isinstance(email_id, str):
                raise ValueError("Invalid email id")
            queued = await run_in_threadpool(enqueue_received, catalog, email_id)
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "Invalid received email id") from None
        # Acknowledge only after durable commit. Body/attachments are fetched by the worker.
        return {"ok": True, "queued": queued}

    @app.get("/subjects")
    def subjects():
        with catalog.connect() as conn:
            return conn.execute("""SELECT s.*,count(a.id) AS activity_count FROM catalog_subjects s
                LEFT JOIN catalog_activity_subjects l ON l.subject_slug=s.slug
                LEFT JOIN catalog_activities a ON a.id=l.activity_id AND a.publication='PUBLISHED' AND a.attendance IN ('OFFLINE','HYBRID')
                GROUP BY s.slug ORDER BY (s.slug='gakumas') DESC,activity_count DESC,s.name""").fetchall()

    @app.get("/activities")
    def activities(
        q: str = Query("", max_length=200),
        subject: str = "",
        kind: str = "",
        city: str = "",
        view: str = "upcoming",
        sort: str = "action",
        page: int = Query(1, ge=1, le=10000),
        limit: int = Query(24, ge=1, le=100),
        from_date: date | None = None,
        to_date: date | None = None,
    ):
        if from_date and to_date and from_date > to_date:
            raise HTTPException(422, "结束日期不能早于开始日期")
        with catalog.connect() as conn:
            clauses = ["a.publication='PUBLISHED'", "a.attendance IN ('OFFLINE','HYBRID')"]
            params = []
            if q:
                clauses.append(
                    "(a.title ILIKE %s OR a.title_zh ILIKE %s OR EXISTS(SELECT 1 FROM catalog_activity_subjects l JOIN catalog_subjects s ON s.slug=l.subject_slug WHERE l.activity_id=a.id AND (s.name ILIKE %s OR s.name_zh ILIKE %s OR s.aliases::text ILIKE %s)))"
                )
                params.extend([f"%{q}%"] * 5)
            if subject:
                clauses.append(
                    "a.id IN (WITH RECURSIVE descendants AS (SELECT slug FROM catalog_subjects WHERE slug=%s UNION SELECT s.slug FROM catalog_subjects s JOIN descendants d ON s.parent_slug=d.slug) SELECT activity_id FROM catalog_activity_subjects WHERE subject_slug IN (SELECT slug FROM descendants))"
                )
                params.append(subject)
            if kind:
                clauses.append("a.kind=%s")
                params.append(kind)
            occurrence_filters = ["o.activity_id=a.id"]
            if city:
                occurrence_filters.append("o.city ILIKE %s")
                params.append(f"%{city}%")
            if from_date:
                occurrence_filters.append(
                    "COALESCE((o.ends_at AT TIME ZONE 'Asia/Tokyo')::date,o.ends_on,(o.starts_at AT TIME ZONE 'Asia/Tokyo')::date,o.starts_on)>=%s"
                )
                params.append(from_date)
            if to_date:
                occurrence_filters.append(
                    "COALESCE((o.starts_at AT TIME ZONE 'Asia/Tokyo')::date,o.starts_on)<=%s"
                )
                params.append(to_date)
            if city or from_date or to_date:
                clauses.append(
                    "EXISTS(SELECT 1 FROM catalog_occurrences o WHERE "
                    + " AND ".join(occurrence_filters)
                    + ")"
                )
            if view != "all":
                clauses.append(
                    "(NOT EXISTS(SELECT 1 FROM catalog_occurrences o WHERE o.activity_id=a.id) OR EXISTS(SELECT 1 FROM catalog_occurrences o WHERE o.activity_id=a.id AND (o.precision='TBD' OR COALESCE(o.ends_at,o.ends_on::timestamptz,o.starts_at,o.starts_on::timestamptz)>=date_trunc('day',NOW() AT TIME ZONE 'Asia/Tokyo') AT TIME ZONE 'Asia/Tokyo')))"
                )
            where = " AND ".join(clauses)
            order = {"recent": "a.updated_at DESC,a.id", "event": "event_on NULLS LAST,a.id"}.get(
                sort, "action_at NULLS LAST,a.updated_at DESC,a.id"
            )
            total = conn.execute(
                f"SELECT count(*) AS count FROM catalog_activities a WHERE {where}", params
            ).fetchone()["count"]
            rows = conn.execute(
                f"""SELECT a.*,(SELECT min({NEXT_ACTION_AT}) FROM catalog_milestones m
                WHERE m.activity_id=a.id AND m.status='CONFIRMED' AND {NEXT_ACTION_AT}>NOW()) AS action_at,
                (SELECT min(COALESCE((o.starts_at AT TIME ZONE 'Asia/Tokyo')::date,o.starts_on)) FROM catalog_occurrences o WHERE o.activity_id=a.id
                  AND COALESCE((o.ends_at AT TIME ZONE 'Asia/Tokyo')::date,o.ends_on,(o.starts_at AT TIME ZONE 'Asia/Tokyo')::date,o.starts_on)>=COALESCE(%s,(NOW() AT TIME ZONE 'Asia/Tokyo')::date)) AS event_on
                FROM catalog_activities a WHERE {where} ORDER BY {order} LIMIT %s OFFSET %s""",
                [from_date, *params, limit, (page - 1) * limit],
            ).fetchall()
            return {"items": hydrate(conn, rows), "total": total, "page": page, "limit": limit}

    @app.get("/activities/{activity_id}")
    def activity(activity_id: str):
        with catalog.connect() as conn:
            row = hydrate(conn, [public_activity(conn, activity_id)])[0]
            activity_id = row["id"]
            row["milestones"] = conn.execute(
                """SELECT m.*,
                COALESCE((SELECT jsonb_agg(occurrence_id) FROM catalog_milestone_scopes s WHERE s.milestone_id=m.id),'[]') AS occurrence_ids,
                EXISTS(SELECT 1 FROM catalog_evidence e WHERE e.milestone_id=m.id AND e.verified) AS verified
                FROM catalog_milestones m WHERE m.activity_id=%s AND m.status<>'SUPERSEDED'
                ORDER BY COALESCE(m.starts_at,(m.starts_on::timestamp AT TIME ZONE 'Asia/Tokyo')),m.id""",
                (activity_id,),
            ).fetchall()
            row["evidence"] = conn.execute(
                "SELECT * FROM catalog_evidence WHERE activity_id=%s ORDER BY observed_at DESC LIMIT 300",
                (activity_id,),
            ).fetchall()
            row["changes"] = conn.execute(
                "SELECT id,kind,summary,before_value,after_value,created_at FROM catalog_changes WHERE activity_id=%s ORDER BY created_at DESC LIMIT 30",
                (activity_id,),
            ).fetchall()
            for change in row["changes"]:
                change["summary"] = change_summary(change["summary"])
            row["relations"] = conn.execute(
                """SELECT a.*,r.kind AS relation_kind FROM catalog_relations r JOIN catalog_activities a ON a.id=r.related_id
                WHERE r.activity_id=%s AND a.publication='PUBLISHED' """,
                (activity_id,),
            ).fetchall()
            return row

    @app.get("/legacy/{slug}")
    def legacy(slug: str):
        with catalog.connect() as conn:
            row = conn.execute(
                "SELECT activity_id FROM catalog_external_ids WHERE key=%s",
                (f"legacy-slug:{slug}",),
            ).fetchone()
            if not row:
                raise HTTPException(404, "旧链接未找到")
            return row

    @app.get("/calendar")
    def calendar(
        from_date: str,
        to_date: str,
        subject: str = "",
        mode: str = "actions",
        page: int = Query(1, ge=1, le=10000),
        limit: int = Query(2000, ge=1, le=2000),
    ):
        try:
            start = datetime.fromisoformat(from_date).replace(tzinfo=ZoneInfo("Asia/Tokyo"))
            end = datetime.fromisoformat(to_date).replace(tzinfo=ZoneInfo("Asia/Tokyo"))
            if not 0 < (end - start).days <= 120:
                raise ValueError()
        except ValueError as exc:
            raise HTTPException(422, "日期范围必须在 1 至 120 天之间") from exc
        with catalog.connect() as conn:
            query = """FROM catalog_milestones m JOIN catalog_activities a ON a.id=m.activity_id
                WHERE a.publication='PUBLISHED' AND a.attendance IN ('OFFLINE','HYBRID') AND m.status IN ('CONFIRMED','CANCELED')
                AND COALESCE(m.starts_at,(m.starts_on::timestamp AT TIME ZONE 'Asia/Tokyo'))<%s
                AND COALESCE(m.ends_at,(m.ends_on::timestamp AT TIME ZONE 'Asia/Tokyo'),m.starts_at,(m.starts_on::timestamp AT TIME ZONE 'Asia/Tokyo'))>=%s
                AND (%s<>'events' OR m.kind IN ('START','PERIOD'))
                AND (%s='' OR a.id IN (WITH RECURSIVE topics AS (SELECT slug FROM catalog_subjects WHERE slug=%s
                  UNION SELECT s.slug FROM catalog_subjects s JOIN topics t ON s.parent_slug=t.slug)
                  SELECT activity_id FROM catalog_activity_subjects WHERE subject_slug IN (SELECT slug FROM topics)))"""
            params = (end, start, mode, subject, subject)
            total = conn.execute("SELECT count(*) AS count " + query, params).fetchone()["count"]
            rows = conn.execute(
                """SELECT m.*,COALESCE(a.title_zh,a.title) AS activity_title,a.kind AS activity_kind,a.status AS activity_status,
                COALESCE((SELECT jsonb_agg(s.subject_slug) FROM catalog_activity_subjects s WHERE s.activity_id=a.id),'[]') AS subjects """
                + query
                + " ORDER BY COALESCE(m.starts_at,(m.starts_on::timestamp AT TIME ZONE 'Asia/Tokyo')),m.id LIMIT %s OFFSET %s",
                (*params, limit, (page - 1) * limit),
            ).fetchall()
            return {
                "items": rows,
                "total": total,
                "page": page,
                "limit": limit,
                "from": from_date,
                "to": to_date,
                "timezone": "Asia/Tokyo",
            }

    @app.post("/auth/login")
    def login(body: LoginBody):
        signing_key()
        email = body.email.strip().lower()
        token = secrets.token_urlsafe(36)
        with catalog.connect() as conn, conn.transaction():
            rate = conn.execute(
                """INSERT INTO genchi_private.auth_limits(key) VALUES(%s) ON CONFLICT(key)
                DO UPDATE SET attempts=CASE WHEN auth_limits.window_start<NOW()-INTERVAL '1 hour' THEN 1 ELSE auth_limits.attempts+1 END,
                window_start=CASE WHEN auth_limits.window_start<NOW()-INTERVAL '1 hour' THEN NOW() ELSE auth_limits.window_start END RETURNING attempts""",
                (digest(email),),
            ).fetchone()
            if rate["attempts"] > 6:
                raise HTTPException(429, "请求较频繁，请稍后再试")
            user = conn.execute(
                """INSERT INTO genchi_private.accounts(id,email,timezone) VALUES(%s,%s,%s)
                ON CONFLICT(email) DO UPDATE SET email=EXCLUDED.email RETURNING *""",
                (uid(), email, body.timezone),
            ).fetchone()
            conn.execute(
                "DELETE FROM genchi_private.login_tokens WHERE account_id=%s", (user["id"],)
            )
            conn.execute(
                "INSERT INTO genchi_private.login_tokens(token_hash,account_id,expires_at) VALUES(%s,%s,NOW()+INTERVAL '20 minutes')",
                (digest(token), user["id"]),
            )
            link = (
                os.getenv("PUBLIC_SITE_URL", "http://localhost:13000")
                + "/zh-Hans/verify?token="
                + token
            )
            enqueue(
                conn,
                account_id=user["id"],
                kind="LOGIN",
                dedup_key=f"login:{digest(token)}",
                due_at=datetime.now(UTC),
                payload={
                    "token_hash": digest(token),
                    "text": f"请确认是你正在登录 Genchi。此链接 20 分钟内有效，只能使用一次：\n\n{link}\n\n如果并非本人操作，请忽略此邮件。",
                },
            )
        return {"sent": True}

    @app.post("/auth/verify")
    def verify(body: TokenBody, response: Response):
        session = secrets.token_urlsafe(40)
        with catalog.connect() as conn, conn.transaction():
            token = conn.execute(
                "DELETE FROM genchi_private.login_tokens WHERE token_hash=%s AND expires_at>NOW() RETURNING account_id",
                (digest(body.token),),
            ).fetchone()
            if not token:
                raise HTTPException(400, "链接已过期或已使用，请重新获取登录邮件")
            conn.execute(
                "UPDATE genchi_private.accounts SET verified_at=COALESCE(verified_at,NOW()) WHERE id=%s",
                (token["account_id"],),
            )
            conn.execute(
                "INSERT INTO genchi_private.sessions VALUES(%s,%s,NOW()+INTERVAL '30 days')",
                (digest(session), token["account_id"]),
            )
        response.set_cookie(
            "genchi_session",
            session,
            max_age=30 * 86400,
            httponly=True,
            secure=os.getenv("PUBLIC_SITE_URL", "").startswith("https:"),
            samesite="lax",
            path="/",
        )
        return {"ok": True}

    @app.post("/auth/logout")
    def logout(request: Request, response: Response):
        with catalog.connect() as conn:
            conn.execute(
                "DELETE FROM genchi_private.sessions WHERE token_hash=%s",
                (digest(request.cookies.get("genchi_session", "")),),
            )
        response.delete_cookie("genchi_session", path="/")
        return {"ok": True}

    @app.post("/auth/unsubscribe")
    def unsubscribe(token: str = Query(..., max_length=200)):
        account_id = verify_unsubscribe(token)
        if not account_id:
            raise HTTPException(400, "退订链接无效")
        with catalog.connect() as conn:
            conn.execute(
                "UPDATE genchi_private.accounts SET unsubscribed=TRUE WHERE id=%s", (account_id,)
            )
        return {"ok": True}

    @app.get("/me")
    def me(request: Request):
        with catalog.connect() as conn:
            user = account(request, conn)
            follows = conn.execute(
                """SELECT f.*,COALESCE(a.title_zh,a.title,s.name_zh,s.name) AS name FROM genchi_private.follows f
                LEFT JOIN catalog_activities a ON f.target_type='ACTIVITY' AND a.id=f.target_id
                LEFT JOIN catalog_subjects s ON f.target_type='SUBJECT' AND s.slug=f.target_id WHERE f.account_id=%s ORDER BY f.created_at DESC""",
                (user["id"],),
            ).fetchall()
            participation = conn.execute(
                "SELECT activity_id,round_key,status FROM genchi_private.participation WHERE account_id=%s",
                (user["id"],),
            ).fetchall()
            return {
                "email": user["email"],
                "timezone": user["timezone"],
                "unsubscribed": user["unsubscribed"],
                "is_admin": user["email"] == os.getenv("ADMIN_EMAIL", ""),
                "follows": follows,
                "participation": participation,
            }

    @app.put("/me/follows")
    def follow(body: FollowBody, request: Request):
        with catalog.connect() as conn, conn.transaction():
            user = account(request, conn)
            if body.target_type == "ACTIVITY":
                body.target_id = public_activity(conn, body.target_id)["id"]
            elif not conn.execute(
                "SELECT slug FROM catalog_subjects WHERE slug=%s", (body.target_id,)
            ).fetchone():
                raise HTTPException(404, "关注主体不存在")
            row = conn.execute(
                """INSERT INTO genchi_private.follows(id,account_id,target_type,target_id,reminder_hours,include_children,kinds,cities)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(account_id,target_type,target_id)
                DO UPDATE SET reminder_hours=EXCLUDED.reminder_hours,include_children=EXCLUDED.include_children,
                kinds=EXCLUDED.kinds,cities=EXCLUDED.cities RETURNING id""",
                (
                    uid(),
                    user["id"],
                    body.target_type,
                    body.target_id,
                    body.reminder_hours,
                    body.include_children,
                    Jsonb(body.kinds),
                    Jsonb(body.cities),
                ),
            )
            conn.execute(
                "UPDATE genchi_private.accounts SET unsubscribed=FALSE WHERE id=%s", (user["id"],)
            )
            return row.fetchone()

    @app.delete("/me/follows/{follow_id}")
    def unfollow(follow_id: str, request: Request):
        with catalog.connect() as conn:
            user = account(request, conn)
            conn.execute(
                "DELETE FROM genchi_private.follows WHERE id=%s AND account_id=%s",
                (follow_id, user["id"]),
            )
            return {"ok": True}

    @app.put("/me/participation/{activity_id}")
    def participate(activity_id: str, body: ParticipationBody, request: Request):
        with catalog.connect() as conn:
            user = account(request, conn)
            activity_id = public_activity(conn, activity_id)["id"]
            if body.status == "WON" and not body.round_key:
                raise HTTPException(422, "请指定中选的票务轮次")
            if (
                body.round_key
                and not conn.execute(
                    "SELECT id FROM catalog_milestones WHERE activity_id=%s AND round_key=%s",
                    (activity_id, body.round_key),
                ).fetchone()
            ):
                raise HTTPException(422, "票务轮次不存在")
            conn.execute(
                """INSERT INTO genchi_private.participation(account_id,activity_id,round_key,status) VALUES(%s,%s,%s,%s)
                ON CONFLICT(account_id,activity_id,round_key) DO UPDATE SET status=EXCLUDED.status""",
                (user["id"], activity_id, body.round_key, body.status),
            )
            return {"ok": True}

    @app.get("/me/notifications")
    def notifications(request: Request):
        with catalog.connect() as conn:
            user = account(request, conn)
            return conn.execute(
                """SELECT q.id,q.kind,q.status,q.due_at,q.sent_at,COALESCE(a.title_zh,a.title) AS activity_title,q.activity_id,
                COALESCE(m.title_zh,m.title) AS milestone_title FROM genchi_private.mail_queue q LEFT JOIN catalog_activities a ON a.id=q.activity_id
                LEFT JOIN catalog_milestones m ON m.id=q.milestone_id WHERE q.account_id=%s AND q.kind<>'LOGIN'
                ORDER BY q.created_at DESC LIMIT 100""",
                (user["id"],),
            ).fetchall()

    @app.delete("/me/participation/{activity_id}")
    def reset_participation(
        activity_id: str, request: Request, round_key: str = Query("", max_length=2000)
    ):
        with catalog.connect() as conn:
            user = account(request, conn)
            activity_id = public_activity(conn, activity_id)["id"]
            conn.execute(
                "DELETE FROM genchi_private.participation WHERE account_id=%s AND activity_id=%s AND round_key=%s",
                (user["id"], activity_id, round_key),
            )
            return {"ok": True}

    @app.get("/me/activities")
    def my_activities(
        request: Request, page: int = Query(1, ge=1, le=10000), limit: int = Query(24, ge=1, le=100)
    ):
        with catalog.connect() as conn:
            user = account(request, conn)
            ids = followed_ids(conn, user["id"])
            rows = conn.execute(
                "SELECT * FROM catalog_activities WHERE id=ANY(%s) ORDER BY updated_at DESC,id LIMIT %s OFFSET %s",
                (ids, limit, (page - 1) * limit),
            ).fetchall()
            return {"items": hydrate(conn, rows), "total": len(ids), "page": page, "limit": limit}

    @app.get("/me/agenda")
    def my_agenda(
        request: Request,
        from_date: date,
        to_date: date,
        page: int = Query(1, ge=1, le=10000),
        limit: int = Query(20, ge=1, le=100),
    ):
        if not 0 < (to_date - from_date).days <= 31:
            raise HTTPException(422, "请选择 1 至 31 天的日程")
        with catalog.connect() as conn:
            user = account(request, conn)
            ids = followed_ids(conn, user["id"])
            nodes = conn.execute(
                """SELECT m.*,COALESCE(a.title_zh,a.title) AS activity_title,a.kind AS activity_kind,a.status AS activity_status,
                COALESCE((SELECT jsonb_agg(occurrence_id) FROM catalog_milestone_scopes s WHERE s.milestone_id=m.id),'[]') AS occurrence_ids,
                EXISTS(SELECT 1 FROM catalog_evidence e WHERE e.milestone_id=m.id AND e.verified) AS verified
                FROM catalog_milestones m JOIN catalog_activities a ON a.id=m.activity_id
                WHERE m.activity_id=ANY(%s) AND m.status IN ('CONFIRMED','CANCELED')""",
                (ids,),
            ).fetchall()
            participation = conn.execute(
                "SELECT activity_id,round_key,status FROM genchi_private.participation WHERE account_id=%s",
                (user["id"],),
            ).fetchall()
            groups, ongoing, undated = agenda_groups(nodes, participation, from_date, to_date)
            updates = conn.execute(
                """SELECT DISTINCT ON (a.id) a.id AS activity_id,COALESCE(a.title_zh,a.title) AS activity_title,a.status,
                c.summary,c.created_at,count(*) OVER(PARTITION BY a.id) AS change_count
                FROM catalog_changes c JOIN catalog_activities a ON a.id=c.activity_id
                WHERE a.id=ANY(%s) AND c.notify AND c.kind NOT IN ('NEW_ACTIVITY','PUBLISHED')
                  AND (c.created_at AT TIME ZONE 'Asia/Tokyo')::date>=%s AND (c.created_at AT TIME ZONE 'Asia/Tokyo')::date<%s
                ORDER BY a.id,c.created_at DESC""",
                (ids, from_date, to_date),
            ).fetchall()
            for update in updates:
                update["summary"] = change_summary(update["summary"])
            return {
                "items": groups[(page - 1) * limit : page * limit],
                "total": len(groups),
                "page": page,
                "limit": limit,
                "action_count": sum(len(g["actions"]) for g in groups),
                "activity_count": len({g["activity_id"] for g in groups}),
                "followed_activity_count": len(ids),
                "ongoing": ongoing,
                "undated_count": undated,
                "updates": updates,
                "from": from_date,
                "to": to_date,
                "timezone": "Asia/Tokyo",
            }

    @app.get("/admin/reviews")
    def reviews(request: Request):
        with catalog.connect() as conn:
            admin(request, conn)
            return conn.execute(
                "SELECT * FROM catalog_reviews WHERE status='PENDING' ORDER BY created_at LIMIT 100"
            ).fetchall()

    @app.post("/admin/reviews/{review_id}")
    def review(review_id: str, body: ReviewBody, request: Request):
        with catalog.connect() as conn:
            reviewer = admin(request, conn)
        try:
            accepted = catalog.approve_review(review_id, reviewer, body.approve, body.activity)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not accepted:
            raise HTTPException(409, "该条目不存在或已经审核")
        return {"ok": True}

    @app.get("/admin/status")
    def status(request: Request):
        with catalog.connect() as conn:
            admin(request, conn)
            return {
                "jobs": conn.execute(
                    "SELECT status,count(*) FROM catalog_jobs GROUP BY status"
                ).fetchall(),
                "mail": conn.execute(
                    "SELECT status,count(*) FROM genchi_private.mail_queue GROUP BY status"
                ).fetchall(),
                "sources": conn.execute(
                    "SELECT source_id,max(observed_at) AS last_observed,count(*) FROM allfeeds.resources GROUP BY source_id ORDER BY source_id"
                ).fetchall(),
            }

    @app.get("/admin/names")
    def name_reviews(
        request: Request,
        state: str = Query("REVIEW", pattern="^(REVIEW|all)$"),
        q: str = Query("", max_length=200),
        page: int = Query(1, ge=1),
        limit: int = Query(20, ge=1, le=100),
    ):
        with catalog.connect() as conn:
            admin(request, conn)
            where = "(%s='all' OR state=%s) AND (%s='' OR source_text ILIKE %s OR display_text ILIKE %s)"
            params = (state, state, q, f"%{q}%", f"%{q}%")
            total = conn.execute(
                "SELECT count(*) AS n FROM catalog_names WHERE " + where, params
            ).fetchone()["n"]
            items = conn.execute(
                "SELECT * FROM catalog_names WHERE "
                + where
                + " ORDER BY entity_type,entity_id LIMIT %s OFFSET %s",
                (*params, limit, (page - 1) * limit),
            ).fetchall()
            return {"items": items, "total": total, "page": page, "limit": limit}

    @app.post("/admin/names")
    def edit_name(body: NameBody, request: Request):
        with catalog.connect() as conn, conn.transaction():
            admin(request, conn)
            try:
                result = sync_name(
                    conn,
                    body.entity_type,
                    body.entity_id,
                    expected_source=body.source_text,
                    approved=body.display_text,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            if result is None:
                raise HTTPException(404, "名称记录不存在")
            return {"ok": True, "item": result}

    return app
