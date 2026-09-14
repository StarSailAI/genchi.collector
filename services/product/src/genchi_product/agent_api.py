"""Authenticated Agent API and sessionless Streamable HTTP MCP surface."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from datetime import date

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import account, rate_limit
from .domain import KINDS
from .naming import change_summary
from .notifications import signing_key
from .presentation import agenda_groups, followed_ids
from .store import uid
from .subscriptions import KIND_LABELS, normalize_keyword

KEY_PATTERN = re.compile(r"^gch_live_([0-9a-f-]{36})\.([A-Za-z0-9_-]{40,80})$")
ALL_SCOPES = {
    "activities:read",
    "updates:read",
    "agenda:read",
    "subscriptions:read",
    "subscriptions:write",
}
READ_SCOPES = ALL_SCOPES - {"subscriptions:write"}
DEFAULT_SCOPES = sorted(READ_SCOPES)


class KeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    scopes: list[str] = Field(default_factory=lambda: DEFAULT_SCOPES.copy(), max_length=10)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("请输入有效的密钥名称")
        return value

    @field_validator("scopes")
    @classmethod
    def known_scopes(cls, value):
        if not value or set(value) - ALL_SCOPES:
            raise ValueError("包含未知权限")
        return sorted(set(value))


class AgentFollow(BaseModel):
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


def _secret_digest(key_id: str, secret: str) -> str:
    return hmac.new(
        signing_key(), f"agent-key:{key_id}:{secret}".encode(), hashlib.sha256
    ).hexdigest()


def _key_view(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "prefix": row["prefix"],
        "scopes": row["scopes"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "last_used_at": row["last_used_at"],
        "revoked_at": row["revoked_at"],
    }


def _identity(request: Request, catalog, scope: str | None):
    raw = request.headers.get("Authorization", "")
    raw = raw[7:] if raw.startswith("Bearer ") else ""
    matched = KEY_PATTERN.fullmatch(raw)
    if not matched:
        raise HTTPException(401, "无效的 API Key", headers={"WWW-Authenticate": "Bearer"})
    key_id, secret = matched.groups()
    with catalog.connect() as conn:
        row = conn.execute(
            """SELECT k.*,a.disabled_at FROM genchi_private.api_keys k
            JOIN genchi_private.accounts a ON a.id=k.account_id
            WHERE k.id=%s AND k.revoked_at IS NULL
              AND (k.expires_at IS NULL OR k.expires_at>NOW())""",
            (key_id,),
        ).fetchone()
    candidate = _secret_digest(key_id, secret)
    expected = row["secret_digest"] if row else "0" * 64
    if not hmac.compare_digest(candidate, expected) or not row or row["disabled_at"]:
        raise HTTPException(401, "无效的 API Key", headers={"WWW-Authenticate": "Bearer"})
    request.state.agent_identity = row
    if scope and scope not in row["scopes"]:
        raise HTTPException(403, f"API Key 缺少权限：{scope}")
    rate_limit(
        catalog,
        [
            ("agent-key-minute", row["id"], 60, 60),
            ("agent-account-minute", row["account_id"], 60, 300),
        ],
    )
    with catalog.connect() as conn:
        conn.execute(
            """UPDATE genchi_private.api_keys SET last_used_at=NOW() WHERE id=%s
            AND (last_used_at IS NULL OR last_used_at<NOW()-INTERVAL '15 minutes')""",
            (row["id"],),
        )
    return row


def _connection_view(row):
    return {
        "name": row["name"],
        "prefix": row["prefix"],
        "scopes": row["scopes"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "last_used_at": row["last_used_at"],
    }


def _cursor_encode(value: int) -> str:
    payload = base64.urlsafe_b64encode(f"v1:{value}".encode()).decode().rstrip("=")
    signature = hmac.new(signing_key(), payload.encode(), hashlib.sha256).hexdigest()[:20]
    return f"{payload}.{signature}"


def _cursor_decode(value: str) -> int:
    if not value:
        return 0
    try:
        payload, signature = value.split(".", 1)
        expected = hmac.new(signing_key(), payload.encode(), hashlib.sha256).hexdigest()[:20]
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
        version, position = decoded.split(":", 1)
        if version != "v1" or int(position) < 0:
            raise ValueError
        return int(position)
    except (ValueError, UnicodeError):
        raise HTTPException(422, "无效的更新游标") from None


def _read_updates(catalog, identity, cursor: str, mode: str, limit: int):
    if mode not in {"following", "all"}:
        raise HTTPException(422, "未知更新模式")
    with catalog.connect() as conn:
        high_water = conn.execute(
            "SELECT COALESCE(MAX(id),0) AS id FROM catalog_changes"
        ).fetchone()["id"]
        if cursor == "now":
            position = high_water
            rows = []
        else:
            position = _cursor_decode(cursor)
            ids = followed_ids(conn, identity["account_id"]) if mode == "following" else None
            rows = (
                []
                if ids == []
                else conn.execute(
                    """SELECT c.id,c.activity_id,c.milestone_id,c.kind,c.summary,c.before_value,
                c.after_value,c.created_at,COALESCE(a.title_zh,a.title) activity_title,
                a.title activity_title_original,a.kind activity_kind,a.status activity_status
                FROM catalog_changes c JOIN catalog_activities a ON a.id=c.activity_id
                WHERE c.id>%s AND c.id<=%s AND a.publication='PUBLISHED'
                  AND a.attendance IN ('OFFLINE','HYBRID')
                  AND (%s::text[] IS NULL OR a.id=ANY(%s::text[]))
                ORDER BY c.id LIMIT %s""",
                    (position, high_water, ids, ids, limit + 1),
                ).fetchall()
            )
        has_more = len(rows) > limit
        items = rows[:limit]
        for item in items:
            item["summary"] = change_summary(item["summary"])
        next_position = items[-1]["id"] if has_more else max(position, high_water)
        return {
            "items": items,
            "next_cursor": _cursor_encode(next_position),
            "has_more": has_more,
            "timezone": "Asia/Tokyo",
        }


def _list_follows(conn, account_id: str):
    return conn.execute(
        """SELECT f.*,CASE WHEN f.target_type='SUBJECT' THEN COALESCE(s.name_zh,s.name)
        WHEN f.target_type='ACTIVITY' THEN COALESCE(a.title_zh,a.title)
        WHEN f.target_type='TAG' THEN f.target_id ELSE f.target_id END AS name
        FROM genchi_private.follows f LEFT JOIN catalog_subjects s
          ON f.target_type='SUBJECT' AND s.slug=f.target_id
        LEFT JOIN catalog_activities a ON f.target_type='ACTIVITY' AND a.id=f.target_id
        WHERE f.account_id=%s ORDER BY f.created_at,f.id""",
        (account_id,),
    ).fetchall()


def _add_follow(conn, account_id: str, body: AgentFollow):
    target_id = body.target_id
    if body.target_type == "KEYWORD":
        try:
            target_id = normalize_keyword(target_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    elif body.target_type == "TAG":
        if target_id not in KIND_LABELS:
            raise HTTPException(422, "请选择已有活动类型标签")
    elif body.target_type == "SUBJECT":
        if not conn.execute(
            "SELECT 1 FROM catalog_subjects WHERE slug=%s", (target_id,)
        ).fetchone():
            raise HTTPException(404, "关注主体不存在")
    elif not conn.execute(
        """SELECT 1 FROM catalog_activities WHERE id=%s AND publication='PUBLISHED'
        AND attendance IN ('OFFLINE','HYBRID')""",
        (target_id,),
    ).fetchone():
        raise HTTPException(404, "活动不存在")
    conn.execute("SELECT id FROM genchi_private.accounts WHERE id=%s FOR UPDATE", (account_id,))
    exists = conn.execute(
        "SELECT id FROM genchi_private.follows WHERE account_id=%s AND target_type=%s AND target_id=%s",
        (account_id, body.target_type, target_id),
    ).fetchone()
    if (
        not exists
        and conn.execute(
            "SELECT count(*) n FROM genchi_private.follows WHERE account_id=%s", (account_id,)
        ).fetchone()["n"]
        >= 500
    ):
        raise HTTPException(422, "关注已达到 500 项，请先整理现有关注")
    return conn.execute(
        """INSERT INTO genchi_private.follows
        (id,account_id,target_type,target_id,reminder_hours,include_children,kinds,cities)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(account_id,target_type,target_id)
        DO UPDATE SET reminder_hours=EXCLUDED.reminder_hours,
        include_children=EXCLUDED.include_children,kinds=EXCLUDED.kinds,cities=EXCLUDED.cities
        RETURNING *""",
        (
            uid(),
            account_id,
            body.target_type,
            target_id,
            body.reminder_hours,
            body.include_children,
            Jsonb(body.kinds),
            Jsonb(body.cities),
        ),
    ).fetchone()


def register_agent_routes(app: FastAPI, catalog, hydrate, public_activity):
    @app.middleware("http")
    async def agent_audit(request: Request, call_next):
        started = time.monotonic()
        response = await call_next(request)
        identity = getattr(request.state, "agent_identity", None)
        if identity and request.url.path.startswith(("/agent/v1", "/mcp")):
            try:
                with catalog.connect() as conn:
                    conn.execute(
                        """INSERT INTO genchi_private.api_key_requests
                        (request_id,api_key_id,account_id,method,route,status_code,elapsed_ms)
                        VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            str(uuid.uuid4()),
                            identity["id"],
                            identity["account_id"],
                            request.method,
                            request.url.path[:200],
                            response.status_code,
                            min(2_147_483_647, int((time.monotonic() - started) * 1000)),
                        ),
                    )
            except Exception:
                pass
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/me/api-keys")
    def list_keys(request: Request):
        with catalog.connect() as conn:
            user = account(request, conn)
            rows = conn.execute(
                "SELECT * FROM genchi_private.api_keys WHERE account_id=%s ORDER BY created_at DESC",
                (user["id"],),
            ).fetchall()
            return [_key_view(row) for row in rows]

    @app.get("/agent/openapi.json", include_in_schema=False)
    def agent_openapi():
        schema = app.openapi()
        paths = {
            path: value
            for path, value in schema.get("paths", {}).items()
            if path.startswith("/agent/v1/")
        }
        for operations in paths.values():
            for operation in operations.values():
                if isinstance(operation, dict):
                    operation["security"] = [{"GenchiApiKey": []}]
        components = schema.setdefault("components", {})
        components.setdefault("securitySchemes", {})["GenchiApiKey"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "gch_live_<key-id>.<secret>",
        }
        return JSONResponse(
            {
                **schema,
                "info": {
                    "title": "Genchi Agent API",
                    "version": "0.1.0",
                    "description": "Structured Japanese offline-event data for personal Agents.",
                },
                "paths": paths,
            },
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.post("/me/api-keys", status_code=201)
    def create_key(body: KeyCreate, request: Request):
        with catalog.connect() as conn, conn.transaction():
            user = account(request, conn)
            conn.execute(
                "SELECT id FROM genchi_private.accounts WHERE id=%s FOR UPDATE", (user["id"],)
            )
            count = conn.execute(
                "SELECT count(*) n FROM genchi_private.api_keys WHERE account_id=%s AND revoked_at IS NULL",
                (user["id"],),
            ).fetchone()["n"]
            if count >= 5:
                raise HTTPException(422, "最多保留 5 枚有效 API Key")
            key_id, secret = uid(), secrets.token_urlsafe(32)
            raw = f"gch_live_{key_id}.{secret}"
            row = conn.execute(
                """INSERT INTO genchi_private.api_keys
                (id,account_id,name,prefix,secret_digest,scopes)
                VALUES(%s,%s,%s,%s,%s,%s) RETURNING *""",
                (
                    key_id,
                    user["id"],
                    body.name,
                    f"gch_live_{key_id[:8]}…",
                    _secret_digest(key_id, secret),
                    Jsonb(body.scopes),
                ),
            ).fetchone()
            return {**_key_view(row), "secret": raw}

    @app.delete("/me/api-keys/{key_id}")
    def revoke_key(key_id: str, request: Request):
        with catalog.connect() as conn:
            user = account(request, conn)
            row = conn.execute(
                """UPDATE genchi_private.api_keys SET revoked_at=COALESCE(revoked_at,NOW())
                WHERE id=%s AND account_id=%s RETURNING id""",
                (key_id, user["id"]),
            ).fetchone()
            if not row:
                raise HTTPException(404, "API Key 不存在")
            return {"ok": True}

    @app.get("/agent/v1/me")
    def agent_me(request: Request):
        return _connection_view(_identity(request, catalog, None))

    @app.get("/agent/v1/subscriptions")
    def subscriptions(request: Request):
        identity = _identity(request, catalog, "subscriptions:read")
        with catalog.connect() as conn:
            return {"items": _list_follows(conn, identity["account_id"])}

    @app.post("/agent/v1/subscriptions", status_code=201)
    def subscribe(body: AgentFollow, request: Request):
        identity = _identity(request, catalog, "subscriptions:write")
        with catalog.connect() as conn, conn.transaction():
            return _add_follow(conn, identity["account_id"], body)

    @app.delete("/agent/v1/subscriptions/{follow_id}")
    def unsubscribe(follow_id: str, request: Request):
        identity = _identity(request, catalog, "subscriptions:write")
        with catalog.connect() as conn:
            row = conn.execute(
                "DELETE FROM genchi_private.follows WHERE id=%s AND account_id=%s RETURNING id",
                (follow_id, identity["account_id"]),
            ).fetchone()
            if not row:
                raise HTTPException(404, "订阅不存在")
            return {"ok": True}

    @app.get("/agent/v1/updates")
    def updates(
        request: Request,
        cursor: str = "",
        limit: int = Query(50, ge=1, le=100),
        mode: str = Query("following", pattern="^(following|all)$"),
    ):
        identity = _identity(request, catalog, "updates:read")
        return _read_updates(catalog, identity, cursor, mode, limit)

    @app.get("/agent/v1/activities")
    def agent_activities(
        request: Request,
        q: str = Query("", max_length=200),
        subject: str = "",
        kind: str = "",
        limit: int = Query(20, ge=1, le=100),
    ):
        _identity(request, catalog, "activities:read")
        clauses = ["a.publication='PUBLISHED'", "a.attendance IN ('OFFLINE','HYBRID')"]
        params = []
        if q:
            clauses.append("(a.title ILIKE %s OR a.title_zh ILIKE %s OR a.summary ILIKE %s)")
            params.extend([f"%{q}%"] * 3)
        if subject:
            clauses.append(
                "EXISTS(SELECT 1 FROM catalog_activity_subjects s WHERE s.activity_id=a.id AND s.subject_slug=%s)"
            )
            params.append(subject)
        if kind:
            if kind not in KINDS:
                raise HTTPException(422, "未知活动类型")
            clauses.append("a.kind=%s")
            params.append(kind)
        with catalog.connect() as conn:
            rows = conn.execute(
                "SELECT a.* FROM catalog_activities a WHERE "
                + " AND ".join(clauses)
                + " ORDER BY a.updated_at DESC,a.id LIMIT %s",
                (*params, limit),
            ).fetchall()
            return {"items": hydrate(conn, rows), "timezone": "Asia/Tokyo"}

    @app.get("/agent/v1/activities/{activity_id}")
    def agent_activity(activity_id: str, request: Request):
        _identity(request, catalog, "activities:read")
        with catalog.connect() as conn:
            row = hydrate(conn, [public_activity(conn, activity_id)])[0]
            row["milestones"] = conn.execute(
                """SELECT m.*,EXISTS(SELECT 1 FROM catalog_evidence e
                WHERE e.milestone_id=m.id AND e.verified) verified
                FROM catalog_milestones m WHERE m.activity_id=%s AND m.status<>'SUPERSEDED'
                ORDER BY COALESCE(m.starts_at,m.starts_on::timestamptz),m.id""",
                (row["id"],),
            ).fetchall()
            row["evidence"] = conn.execute(
                """SELECT id,milestone_id,url,excerpt,method,verified,observed_at
                FROM catalog_evidence WHERE activity_id=%s ORDER BY observed_at DESC LIMIT 100""",
                (row["id"],),
            ).fetchall()
            return row

    @app.get("/agent/v1/agenda")
    def agent_agenda(
        request: Request,
        from_date: date,
        to_date: date,
        limit: int = Query(100, ge=1, le=200),
    ):
        identity = _identity(request, catalog, "agenda:read")
        if not 0 < (to_date - from_date).days <= 31:
            raise HTTPException(422, "请选择 1 至 31 天的日程")
        with catalog.connect() as conn:
            ids = followed_ids(conn, identity["account_id"])
            nodes = (
                []
                if not ids
                else conn.execute(
                    """SELECT m.*,a.title activity_title_original,COALESCE(a.title_zh,a.title) activity_title,
                a.kind activity_kind,a.status activity_status,
                EXISTS(SELECT 1 FROM catalog_evidence e WHERE e.milestone_id=m.id AND e.verified) verified
                FROM catalog_milestones m JOIN catalog_activities a ON a.id=m.activity_id
                WHERE m.activity_id=ANY(%s) AND m.status IN ('CONFIRMED','CANCELED')""",
                    (ids,),
                ).fetchall()
            )
            participation = conn.execute(
                "SELECT activity_id,round_key,status FROM genchi_private.participation WHERE account_id=%s",
                (identity["account_id"],),
            ).fetchall()
            groups, ongoing, undated = agenda_groups(nodes, participation, from_date, to_date)
            return {
                "items": groups[:limit],
                "total": len(groups),
                "ongoing": ongoing,
                "undated_count": undated,
                "from": from_date,
                "to": to_date,
                "timezone": "Asia/Tokyo",
            }

    tool_specs = [
        (
            "get_connection_info",
            "检查当前 API Key 的权限与有效期",
            {},
            True,
        ),
        (
            "search_activities",
            "查询日本线下活动",
            {"q": {"type": "string"}, "subject": {"type": "string"}, "kind": {"type": "string"}},
            True,
        ),
        (
            "get_activity_timeline",
            "读取活动的完整时间线和官方证据",
            {"activity_id": {"type": "string"}},
            True,
        ),
        (
            "get_latest_updates",
            "读取上次游标之后的活动变化",
            {
                "cursor": {"type": "string"},
                "mode": {"type": "string", "enum": ["following", "all"]},
            },
            True,
        ),
        ("list_subscriptions", "查看用户当前订阅", {}, True),
        (
            "create_subscription",
            "新增或更新一个活动、系列、关键词或类型订阅",
            {
                "target_type": {
                    "type": "string",
                    "enum": ["SUBJECT", "ACTIVITY", "KEYWORD", "TAG"],
                },
                "target_id": {"type": "string"},
                "reminder_hours": {
                    "type": "integer",
                    "enum": [0, 2, 24, 48],
                    "default": 24,
                },
                "include_children": {"type": "boolean", "default": True},
                "kinds": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(KINDS)},
                    "maxItems": 8,
                },
                "cities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                },
            },
            False,
        ),
        ("delete_subscription", "删除一个订阅", {"follow_id": {"type": "string"}}, False),
        (
            "get_agenda",
            "读取一段日期内与订阅相关的日程",
            {
                "from_date": {"type": "string", "format": "date"},
                "to_date": {"type": "string", "format": "date"},
            },
            True,
        ),
    ]

    def mcp_result(value):
        text = json.dumps(value, ensure_ascii=False, default=str)
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": json.loads(text),
        }

    @app.post("/mcp")
    def mcp(request: Request, body: dict):
        method, request_id = body.get("method"), body.get("id")
        if method == "initialize":
            _identity(request, catalog, None)
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "genchi", "version": "0.1.0"},
                "instructions": "Use Genchi for structured Japanese offline-event facts. Preserve JST, date precision and official evidence. Never invent missing times. Ask before changing subscriptions unless the user already requested it.",
            }
        elif method == "notifications/initialized":
            _identity(request, catalog, None)
            return Response(status_code=202)
        elif method == "tools/list":
            _identity(request, catalog, None)
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": description,
                        "inputSchema": {
                            "type": "object",
                            "properties": properties,
                            "required": [
                                key
                                for key in properties
                                if key
                                in {
                                    "activity_id",
                                    "target_type",
                                    "target_id",
                                    "follow_id",
                                    "from_date",
                                    "to_date",
                                }
                            ],
                        },
                        "annotations": {"readOnlyHint": readonly},
                    }
                    for name, description, properties, readonly in tool_specs
                ]
            }
        elif method == "tools/call":
            params, args = (
                body.get("params") or {},
                (body.get("params") or {}).get("arguments") or {},
            )
            name = params.get("name")
            if name == "get_connection_info":
                value = _connection_view(_identity(request, catalog, None))
            elif name == "search_activities":
                identity = _identity(request, catalog, "activities:read")
                del identity
                clauses, values = (
                    ["a.publication='PUBLISHED'", "a.attendance IN ('OFFLINE','HYBRID')"],
                    [],
                )
                if args.get("q"):
                    clauses.append(
                        "(a.title ILIKE %s OR a.title_zh ILIKE %s OR a.summary ILIKE %s)"
                    )
                    values.extend([f"%{args['q']}%"] * 3)
                if args.get("subject"):
                    clauses.append(
                        "EXISTS(SELECT 1 FROM catalog_activity_subjects s WHERE s.activity_id=a.id AND s.subject_slug=%s)"
                    )
                    values.append(args["subject"])
                if args.get("kind"):
                    clauses.append("a.kind=%s")
                    values.append(args["kind"])
                with catalog.connect() as conn:
                    rows = conn.execute(
                        "SELECT a.* FROM catalog_activities a WHERE "
                        + " AND ".join(clauses)
                        + " ORDER BY updated_at DESC LIMIT 20",
                        values,
                    ).fetchall()
                    value = {"items": hydrate(conn, rows), "timezone": "Asia/Tokyo"}
            elif name == "get_activity_timeline":
                _identity(request, catalog, "activities:read")
                with catalog.connect() as conn:
                    row = hydrate(conn, [public_activity(conn, str(args.get("activity_id", "")))])[
                        0
                    ]
                    row["milestones"] = conn.execute(
                        "SELECT * FROM catalog_milestones WHERE activity_id=%s AND status<>'SUPERSEDED' ORDER BY COALESCE(starts_at,starts_on::timestamptz),id",
                        (row["id"],),
                    ).fetchall()
                    row["evidence"] = conn.execute(
                        "SELECT id,milestone_id,url,excerpt,verified,observed_at FROM catalog_evidence WHERE activity_id=%s ORDER BY observed_at DESC LIMIT 100",
                        (row["id"],),
                    ).fetchall()
                    value = row
            elif name == "get_latest_updates":
                identity = _identity(request, catalog, "updates:read")
                value = _read_updates(
                    catalog,
                    identity,
                    str(args.get("cursor", "")),
                    str(args.get("mode", "following")),
                    50,
                )
            elif name == "list_subscriptions":
                identity = _identity(request, catalog, "subscriptions:read")
                with catalog.connect() as conn:
                    value = {"items": _list_follows(conn, identity["account_id"])}
            elif name == "create_subscription":
                identity = _identity(request, catalog, "subscriptions:write")
                with catalog.connect() as conn, conn.transaction():
                    value = _add_follow(conn, identity["account_id"], AgentFollow(**args))
            elif name == "delete_subscription":
                identity = _identity(request, catalog, "subscriptions:write")
                with catalog.connect() as conn:
                    deleted = conn.execute(
                        "DELETE FROM genchi_private.follows WHERE id=%s AND account_id=%s RETURNING id",
                        (args.get("follow_id"), identity["account_id"]),
                    ).fetchone()
                    if not deleted:
                        raise HTTPException(404, "订阅不存在")
                    value = {"ok": True}
            elif name == "get_agenda":
                identity = _identity(request, catalog, "agenda:read")
                start, end = (
                    date.fromisoformat(args["from_date"]),
                    date.fromisoformat(args["to_date"]),
                )
                if not 0 < (end - start).days <= 31:
                    raise HTTPException(422, "请选择 1 至 31 天的日程")
                with catalog.connect() as conn:
                    ids = followed_ids(conn, identity["account_id"])
                    nodes = (
                        []
                        if not ids
                        else conn.execute(
                            """SELECT m.*,a.title activity_title_original,COALESCE(a.title_zh,a.title) activity_title,a.kind activity_kind,a.status activity_status,EXISTS(SELECT 1 FROM catalog_evidence e WHERE e.milestone_id=m.id AND e.verified) verified FROM catalog_milestones m JOIN catalog_activities a ON a.id=m.activity_id WHERE m.activity_id=ANY(%s) AND m.status IN ('CONFIRMED','CANCELED')""",
                            (ids,),
                        ).fetchall()
                    )
                    participation = conn.execute(
                        "SELECT activity_id,round_key,status FROM genchi_private.participation WHERE account_id=%s",
                        (identity["account_id"],),
                    ).fetchall()
                    groups, ongoing, undated = agenda_groups(nodes, participation, start, end)
                    value = {
                        "items": groups,
                        "ongoing": ongoing,
                        "undated_count": undated,
                        "timezone": "Asia/Tokyo",
                    }
            else:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": "Unknown tool"},
                    },
                    status_code=404,
                )
            result = mcp_result(value)
        else:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"},
                },
                status_code=404,
            )
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            headers={"MCP-Protocol-Version": "2025-06-18", "Cache-Control": "no-store"},
        )

    @app.get("/mcp")
    def mcp_get():
        return Response(status_code=405, headers={"Allow": "POST"})
