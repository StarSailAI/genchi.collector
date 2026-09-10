"""Email challenges and revocable sessions. Secrets never enter persisted mail payloads."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import time
import unicodedata
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .emails import render_login_email
from .localization import Locale
from .notifications import signing_key, smtp_send
from .store import uid

COOKIE = "genchi_session"
CODE_TTL = 15 * 60
SESSION_SECONDS = 30 * 86400


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_email(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("请输入有效的邮箱地址")
    value = unicodedata.normalize("NFKC", value).strip().lower()
    if value.count("@") != 1:
        raise ValueError("请输入有效的邮箱地址")
    local, domain = value.split("@")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("请输入有效的邮箱地址") from exc
    value = local + "@" + domain
    if (
        len(value) > 254
        or len(local) > 64
        or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or "." not in domain
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in domain.split(".")
        )
    ):
        raise ValueError("请输入有效的邮箱地址")
    return value


def valid_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except Exception as exc:
        raise ValueError("请选择有效的时区") from exc
    return value


class EmailBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)
    locale: Locale = "zh-Hans"
    timezone: str = Field(default="Asia/Shanghai", max_length=80)

    _email = field_validator("email", mode="before")(normalize_email)
    _timezone = field_validator("timezone")(valid_timezone)


class VerifyBody(EmailBody):
    code: str = Field(pattern=r"^[0-9]{6}$", min_length=6, max_length=6)


def cookie_options():
    return dict(
        httponly=True,
        secure=os.getenv("PUBLIC_SITE_URL", "").startswith("https:"),
        samesite="lax",
        path="/",
    )


def clear_cookie(response: Response):
    response.delete_cookie(COOKIE, **cookie_options())


def account(request: Request, conn):
    raw = request.cookies.get(COOKIE, "")
    if not 20 <= len(raw) <= 200:
        raise HTTPException(401, "请先验证邮箱并登录")
    user = conn.execute(
        """SELECT a.* FROM genchi_private.sessions s JOIN genchi_private.accounts a ON a.id=s.account_id
        WHERE s.token_hash=%s AND s.expires_at>NOW() AND a.verified_at IS NOT NULL AND a.disabled_at IS NULL""",
        (digest(raw),),
    ).fetchone()
    if not user:
        raise HTTPException(401, "登录已失效，请重新验证邮箱")
    return user


def source_key(request: Request) -> str:
    """Only the BFF's authenticated attribution can override the actual peer."""
    claimed = request.headers.get("X-Genchi-Client-IP")
    if claimed is not None:
        key = os.getenv("PRODUCT_PROXY_SECRET", "")
        stamp = request.headers.get("X-Genchi-Proxy-Time", "")
        signature = request.headers.get("X-Genchi-Proxy-Signature", "")
        message = f"{stamp}\n{request.method}\n{request.url.path}\n{claimed}"
        expected = hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
        if (
            len(key) < 32
            or len(claimed) > 80
            or not stamp.isdigit()
            or len(stamp) > 12
            or abs(time.time() - int(stamp)) > 60
            or not hmac.compare_digest(signature, expected)
        ):
            raise HTTPException(403, "请求来源验证失败")
        raw = claimed
    else:
        raw = request.client.host if request.client else "unknown"
    try:
        address = ipaddress.ip_address(raw)
        if address.version == 6:
            raw = str(ipaddress.ip_network(f"{address}/64", strict=False))
        else:
            raw = str(address)
    except ValueError:
        raw = "shared-web" if claimed else "unknown"
    return hmac.new(signing_key(), f"source:{raw}".encode(), hashlib.sha256).hexdigest()


def code_digest(challenge_id: str, code: str) -> str:
    return hmac.new(
        signing_key(), f"login:{challenge_id}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def rate_limit(catalog, limits: list[tuple[str, str, int, int]]):
    """Counters commit before errors; all processes share the same atomic buckets."""
    retry = 0
    buckets = sorted(
        (
            hmac.new(signing_key(), f"rate:{scope}:{value}".encode(), hashlib.sha256).hexdigest(),
            seconds,
            maximum,
        )
        for scope, value, seconds, maximum in limits
    )
    with catalog.connect() as conn:
        for key, seconds, maximum in buckets:
            row = conn.execute(
                """INSERT INTO genchi_private.auth_limits(key) VALUES(%s) ON CONFLICT(key)
                DO UPDATE SET attempts=CASE WHEN auth_limits.window_start<=NOW()-(%s*INTERVAL '1 second') THEN 1 ELSE LEAST(auth_limits.attempts,1000000)+1 END,
                window_start=CASE WHEN auth_limits.window_start<=NOW()-(%s*INTERVAL '1 second') THEN NOW() ELSE auth_limits.window_start END
                RETURNING attempts,GREATEST(1,ceil(extract(epoch FROM window_start+(%s*INTERVAL '1 second')-NOW())))::int AS retry""",
                (key, seconds, seconds, seconds),
            ).fetchone()
            if row["attempts"] > maximum:
                retry = max(retry, row["retry"])
    if retry:
        raise HTTPException(429, "请求较频繁，请稍后再试", headers={"Retry-After": str(retry)})


def can_login(user):
    if user and user["disabled_at"]:
        return False
    return (
        bool(user and user["verified_at"])
        or os.getenv("AUTH_REGISTRATION_OPEN", "true").lower() == "true"
    )


def send_code(catalog, body: EmailBody, request: Request):
    source = source_key(request)
    rate_limit(
        catalog, [("send-source-minute", source, 60, 5), ("send-source-hour", source, 3600, 20)]
    )
    rate_limit(
        catalog,
        [("send-email-minute", body.email, 60, 1), ("send-email-hour", body.email, 3600, 6)],
    )
    with catalog.connect() as conn:
        user = conn.execute(
            "SELECT * FROM genchi_private.accounts WHERE email=%s", (body.email,)
        ).fetchone()
    if not can_login(user):
        raise HTTPException(403, "此邮箱暂时无法登录")
    rate_limit(
        catalog, [("send-global-hour", "all", 3600, 100), ("send-global-day", "all", 86400, 500)]
    )
    challenge_id = uid()
    with catalog.connect() as conn:
        old = conn.execute(
            "SELECT id,code_digest FROM genchi_private.email_challenges WHERE email=%s FOR UPDATE",
            (body.email,),
        ).fetchone()
        code = f"{secrets.randbelow(1_000_000):06d}"
        while old and hmac.compare_digest(old["code_digest"], code_digest(old["id"], code)):
            code = f"{secrets.randbelow(1_000_000):06d}"
        conn.execute(
            """INSERT INTO genchi_private.email_challenges(email,id,code_digest,timezone,locale,status,expires_at)
            VALUES(%s,%s,%s,%s,%s,'PENDING',NOW()+(%s*INTERVAL '1 second'))
            ON CONFLICT(email) DO UPDATE SET id=EXCLUDED.id,code_digest=EXCLUDED.code_digest,timezone=EXCLUDED.timezone,locale=EXCLUDED.locale,
            status='PENDING',attempts=0,created_at=NOW(),expires_at=EXCLUDED.expires_at,sent_at=NULL,consumed_at=NULL""",
            (
                body.email,
                challenge_id,
                code_digest(challenge_id, code),
                body.timezone,
                body.locale,
                CODE_TTL,
            ),
        )
        conn.execute(
            "DELETE FROM genchi_private.email_challenges WHERE expires_at<NOW()-INTERVAL '1 day'"
        )
        conn.execute(
            "DELETE FROM genchi_private.auth_limits WHERE window_start<NOW()-INTERVAL '2 days'"
        )
        conn.execute("DELETE FROM genchi_private.sessions WHERE expires_at<=NOW()")
    try:
        message = render_login_email(
            code,
            expires_minutes=CODE_TTL // 60,
            locale=body.locale,
            site_url=os.getenv("PUBLIC_SITE_URL", "http://localhost:13000"),
        )
        smtp_send(
            body.email,
            message.subject,
            message.text,
            "auth-" + challenge_id,
            None,
            timeout=10,
            html=message.html,
        )
    except Exception:
        with catalog.connect() as conn:
            conn.execute(
                "UPDATE genchi_private.email_challenges SET status='FAILED',consumed_at=NOW() WHERE id=%s",
                (challenge_id,),
            )
        raise HTTPException(503, "验证码暂时发送失败，请稍后重新获取") from None
    with catalog.connect() as conn:
        updated = conn.execute(
            "UPDATE genchi_private.email_challenges SET status='SENT',sent_at=NOW() WHERE id=%s AND status='PENDING' AND expires_at>NOW()",
            (challenge_id,),
        ).rowcount
    if not updated:
        raise HTTPException(503, "验证码已失效，请重新获取")
    return {"ok": True, "auth_method": "email_code", "expires_in": CODE_TTL, "retry_after": 60}


def verify_code(catalog, body: VerifyBody, request: Request, response: Response):
    source = source_key(request)
    rate_limit(catalog, [("verify-source", source, CODE_TTL, 50)])
    rate_limit(
        catalog,
        [
            ("verify-email", body.email, CODE_TTL, 15),
            ("verify-pair", body.email + ":" + source, CODE_TTL, 10),
        ],
    )
    error = None
    session = secrets.token_urlsafe(40)
    with catalog.connect() as conn:
        challenge = conn.execute(
            "SELECT * FROM genchi_private.email_challenges WHERE email=%s FOR UPDATE", (body.email,)
        ).fetchone()
        if (
            not challenge
            or challenge["status"] != "SENT"
            or challenge["expires_at"] <= datetime.now(UTC)
        ):
            error = HTTPException(400, "验证码错误或已失效，请重新获取")
        elif not hmac.compare_digest(
            challenge["code_digest"], code_digest(challenge["id"], body.code)
        ):
            conn.execute(
                """UPDATE genchi_private.email_challenges SET attempts=attempts+1,
                status=CASE WHEN attempts>=4 THEN 'CONSUMED' ELSE status END,
                consumed_at=CASE WHEN attempts>=4 THEN NOW() ELSE consumed_at END WHERE id=%s""",
                (challenge["id"],),
            )
            error = HTTPException(
                400,
                "验证码错误或已失效，请重新获取"
                if challenge["attempts"] >= 4
                else "验证码不正确，请检查后重试",
            )
        else:
            user = conn.execute(
                "SELECT * FROM genchi_private.accounts WHERE email=%s FOR UPDATE", (body.email,)
            ).fetchone()
            if not can_login(user):
                error = HTTPException(403, "此邮箱暂时无法登录")
            else:
                is_new_user = not user or not user["verified_at"]
                user = conn.execute(
                    """INSERT INTO genchi_private.accounts(id,email,timezone,locale,verified_at,last_login_at)
                    VALUES(%s,%s,%s,%s,NOW(),NOW()) ON CONFLICT(email) DO UPDATE SET
                    verified_at=COALESCE(accounts.verified_at,NOW()),last_login_at=NOW(),locale=EXCLUDED.locale,updated_at=NOW() RETURNING id""",
                    (uid(), body.email, challenge["timezone"], body.locale),
                ).fetchone()
                conn.execute(
                    "DELETE FROM genchi_private.sessions WHERE token_hash=%s",
                    (digest(request.cookies.get(COOKIE, "")),),
                )
                conn.execute(
                    "INSERT INTO genchi_private.sessions(token_hash,account_id,expires_at) VALUES(%s,%s,NOW()+(%s*INTERVAL '1 second'))",
                    (digest(session), user["id"], SESSION_SECONDS),
                )
            conn.execute(
                "UPDATE genchi_private.email_challenges SET status='CONSUMED',consumed_at=NOW() WHERE id=%s",
                (challenge["id"],),
            )
    if error:
        raise error
    response.set_cookie(COOKIE, session, max_age=SESSION_SECONDS, **cookie_options())
    return {"ok": True, "is_new_user": is_new_user}


def register_auth_routes(app, catalog):
    @app.post("/auth/login")
    def login(body: EmailBody, request: Request):
        return send_code(catalog, body, request)

    @app.post("/auth/verify")
    def verify(body: VerifyBody, request: Request, response: Response):
        return verify_code(catalog, body, request, response)

    @app.post("/auth/logout")
    def logout(request: Request, response: Response):
        with catalog.connect() as conn:
            conn.execute(
                "DELETE FROM genchi_private.sessions WHERE token_hash=%s",
                (digest(request.cookies.get(COOKIE, "")),),
            )
        clear_cookie(response)
        return {"ok": True}

    @app.post("/auth/logout-all")
    def logout_all(request: Request, response: Response):
        with catalog.connect() as conn:
            user = account(request, conn)
            conn.execute("DELETE FROM genchi_private.sessions WHERE account_id=%s", (user["id"],))
        clear_cookie(response)
        return {"ok": True}
