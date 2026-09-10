from __future__ import annotations

import os
import secrets
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool
from svix.webhooks import Webhook, WebhookVerificationError

from .auth import COOKIE, account, clear_cookie, register_auth_routes, valid_timezone
from .auth import digest as digest
from .domain import KINDS, ActivityInput
from .inbound import enqueue_received
from .localization import (
    LOCALES,
    Locale,
    LocalizedJSONResponse,
    locale_of,
    localize_catalog,
    request_locale,
    translate,
)
from .naming import change_summary, sync_name
from .notifications import verify_unsubscribe
from .presentation import agenda_groups, followed_ids
from .store import Catalog, uid
from .subscriptions import KIND_LABELS, normalize_keyword

# DATE anchors are only for sorting; the public precision and calendar values remain DATE.
NEXT_ACTION_AT = """(CASE WHEN COALESCE(m.starts_at,m.starts_on::timestamp AT TIME ZONE 'Asia/Tokyo')>NOW()
  THEN COALESCE(m.starts_at,m.starts_on::timestamp AT TIME ZONE 'Asia/Tokyo')
  ELSE COALESCE(m.ends_at,(m.ends_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo',m.starts_at,(m.starts_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo') END)"""


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
    fresh_time_ids = {
        row["activity_id"]
        for row in conn.execute(
            """SELECT DISTINCT activity_id FROM catalog_changes
            WHERE activity_id=ANY(%s)
            AND (created_at AT TIME ZONE 'Asia/Tokyo')::date=
                (NOW() AT TIME ZONE 'Asia/Tokyo')::date
            AND (kind IN ('NEW_ACTIVITY','NEW_MILESTONE') OR
              (kind='MILESTONE_CHANGED' AND (
                before_value->'starts_at' IS DISTINCT FROM after_value->'starts_at' OR
                before_value->'ends_at' IS DISTINCT FROM after_value->'ends_at' OR
                before_value->'starts_on' IS DISTINCT FROM after_value->'starts_on' OR
                before_value->'ends_on' IS DISTINCT FROM after_value->'ends_on' OR
                before_value->'precision' IS DISTINCT FROM after_value->'precision'
              )))""",
            (ids,),
        ).fetchall()
    }
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
        row["has_new_time_today"] = row["id"] in fresh_time_ids
        row.update(
            next(
                (c for c in counts if c["activity_id"] == row["id"]),
                {"source_count": 0, "checked_at": None},
            )
        )
    return rows


class FollowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_type: str = Field(pattern="^(SUBJECT|ACTIVITY|KEYWORD|TAG)$")
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


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(max_length=60)
    timezone: str = Field(max_length=80)
    unsubscribed: bool
    locale: Locale | None = None

    _timezone = field_validator("timezone")(valid_timezone)

    @field_validator("display_name")
    @classmethod
    def name(cls, value):
        value = value.strip()
        if value and not value.isprintable():
            raise ValueError("昵称不能包含控制字符")
        return value


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
    app = FastAPI(
        title="Genchi Product", version="0.2.0", default_response_class=LocalizedJSONResponse
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/auth/"):
            return JSONResponse(
                {"detail": translate("请检查邮箱地址和六位数字验证码")},
                status_code=422,
                headers={"Cache-Control": "no-store"},
            )
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse(
            {"detail": translate(exc.detail) if isinstance(exc.detail, str) else exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.middleware("http")
    async def language_context(request: Request, call_next):
        locale = locale_of(
            request.query_params.get("locale") or request.headers.get("X-Genchi-Locale")
        )
        token = request_locale.set(locale)
        projection = localize_catalog.set(
            request.method == "GET" and not request.url.path.startswith(("/admin", "/health"))
        )
        try:
            response = await call_next(request)
            response.headers["Content-Language"] = locale
            response.headers.append("Vary", "X-Genchi-Locale")
            return response
        finally:
            request_locale.reset(token)
            localize_catalog.reset(projection)

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            expected = os.getenv("PUBLIC_SITE_URL", "http://localhost:13000").rstrip("/")
            protected = request.url.path.startswith("/me") or request.url.path in {
                "/auth/login",
                "/auth/verify",
                "/auth/logout",
                "/auth/logout-all",
            }
            if (
                (protected and origin != expected)
                or (origin and origin.rstrip("/") != expected)
                or request.headers.get("Sec-Fetch-Site") == "cross-site"
            ):
                return Response("Origin not allowed", status_code=403)
        response = await call_next(request)
        if request.url.path.startswith(("/auth", "/me", "/admin")):
            response.headers["Cache-Control"] = "no-store"
        if response.status_code == 401 and request.cookies.get(COOKIE):
            clear_cookie(response)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health")
    def health():
        with catalog.connect() as conn:
            contract = conn.execute(
                'SELECT major,minor FROM "SchemaContract" WHERE id=1'
            ).fetchone()
            ready = bool(contract and contract["major"] == 1 and contract["minor"] >= 6)
            if not ready:
                raise HTTPException(503, "Catalog schema 1.6 required")
            return {
                "ok": True,
                "schema": f"{contract['major']}.{contract['minor']}",
                "service": "genchi-product",
                "auth_method": "email_code",
                "locales": list(LOCALES),
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
                """SELECT m.*,a.title AS activity_title_original,COALESCE(a.title_zh,a.title) AS activity_title,a.kind AS activity_kind,a.status AS activity_status,
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

    register_auth_routes(app, catalog)

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
            conn.autocommit = True
            conn.execute("DELETE FROM genchi_private.sessions WHERE expires_at<=NOW()")
            user = account(request, conn)
            follows = conn.execute(
                """SELECT f.*,COALESCE(a.title,s.name,f.target_id) AS name_original,COALESCE(a.title_zh,a.title,s.name_zh,s.name,f.target_id) AS name FROM genchi_private.follows f
                LEFT JOIN catalog_activities a ON f.target_type='ACTIVITY' AND a.id=f.target_id
                LEFT JOIN catalog_subjects s ON f.target_type='SUBJECT' AND s.slug=f.target_id WHERE f.account_id=%s ORDER BY f.created_at DESC""",
                (user["id"],),
            ).fetchall()
            for follow in follows:
                if follow["target_type"] == "TAG":
                    follow["name"] = KIND_LABELS.get(follow["target_id"], follow["target_id"])
            participation = conn.execute(
                "SELECT activity_id,round_key,status FROM genchi_private.participation WHERE account_id=%s",
                (user["id"],),
            ).fetchall()
            return {
                "id": user["id"],
                "email": user["email"],
                "display_name": user["display_name"],
                "created_at": user["created_at"],
                "timezone": user["timezone"],
                "locale": user["locale"],
                "unsubscribed": user["unsubscribed"],
                "is_admin": user["email"] == os.getenv("ADMIN_EMAIL", ""),
                "follows": follows,
                "participation": participation,
            }

    @app.put("/me")
    def profile(body: ProfileBody, request: Request):
        with catalog.connect() as conn:
            user = account(request, conn)
            conn.execute(
                "UPDATE genchi_private.accounts SET display_name=%s,timezone=%s,unsubscribed=%s,locale=COALESCE(%s,locale),updated_at=NOW() WHERE id=%s",
                (body.display_name, body.timezone, body.unsubscribed, body.locale, user["id"]),
            )
        return {"ok": True}

    @app.get("/tags")
    def tags():
        with catalog.connect() as conn:
            counts = {
                row["kind"]: row["n"]
                for row in conn.execute(
                    "SELECT kind,count(*) n FROM catalog_activities WHERE publication='PUBLISHED' AND attendance IN ('OFFLINE','HYBRID') GROUP BY kind"
                ).fetchall()
            }
        return [
            {"slug": slug, "name": name, "activity_count": counts.get(slug, 0)}
            for slug, name in KIND_LABELS.items()
        ]

    @app.put("/me/follows")
    def follow(body: FollowBody, request: Request):
        with catalog.connect() as conn, conn.transaction():
            user = account(request, conn)
            if body.target_type == "ACTIVITY":
                body.target_id = public_activity(conn, body.target_id)["id"]
            elif body.target_type == "KEYWORD":
                try:
                    body.target_id = normalize_keyword(body.target_id)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from None
            elif body.target_type == "TAG":
                if body.target_id not in KIND_LABELS:
                    raise HTTPException(422, "请选择已有活动类型标签")
            elif not conn.execute(
                "SELECT slug FROM catalog_subjects WHERE slug=%s", (body.target_id,)
            ).fetchone():
                raise HTTPException(404, "关注主体不存在")
            conn.execute(
                "SELECT id FROM genchi_private.accounts WHERE id=%s FOR UPDATE", (user["id"],)
            )
            exists = conn.execute(
                "SELECT id FROM genchi_private.follows WHERE account_id=%s AND target_type=%s AND target_id=%s",
                (user["id"], body.target_type, body.target_id),
            ).fetchone()
            if (
                not exists
                and conn.execute(
                    "SELECT count(*) n FROM genchi_private.follows WHERE account_id=%s",
                    (user["id"],),
                ).fetchone()["n"]
                >= 500
            ):
                raise HTTPException(422, "关注已达到 500 项，请先整理现有关注")
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
                """SELECT q.id,q.kind,q.status,q.due_at,q.sent_at,a.title AS activity_title_original,COALESCE(a.title_zh,a.title) AS activity_title,q.activity_id,
                m.title AS milestone_title_original,COALESCE(m.title_zh,m.title) AS milestone_title FROM genchi_private.mail_queue q LEFT JOIN catalog_activities a ON a.id=q.activity_id
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
                """SELECT m.*,a.title AS activity_title_original,COALESCE(a.title_zh,a.title) AS activity_title,a.kind AS activity_kind,a.status AS activity_status,
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
                """SELECT DISTINCT ON (a.id) a.id AS activity_id,a.title AS activity_title_original,COALESCE(a.title_zh,a.title) AS activity_title,a.status,
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
