"""Resend inbound webhook outbox. Mail contents never enter the public catalog."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import socket
import time
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses, parseaddr
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import requests
from psycopg.types.json import Jsonb

from .notifications import signing_key
from .store import Catalog

# Leave room for MIME overhead within Resend's 40 MB send limit.
MAX_MAIL_BYTES = 38_000_000
API_ROOT = "https://api.resend.com"
LOG = logging.getLogger(__name__)


class InboundError(Exception):
    """Only a safe error code, never mail content, URLs or credentials."""

    def __init__(self, code: str, *, permanent=False):
        super().__init__(code)
        self.permanent = permanent


class Resend:
    def __init__(self):
        self.key = os.environ.get("RESEND_API_KEY", "")
        if not self.key:
            raise InboundError("missing_resend_api_key")
        self.last_request = 0.0

    def request(self, method: str, path: str, *, payload=None, idempotency_key=None):
        # Keep below the default account rate limit, including attachment requests.
        time.sleep(max(0, 0.65 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        headers = {"Authorization": f"Bearer {self.key}", "User-Agent": "Genchi inbound"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            with requests.request(
                method,
                API_ROOT + path,
                json=payload,
                headers=headers,
                timeout=(10, 45),
                allow_redirects=False,
                stream=True,
            ) as response:
                if not 200 <= response.status_code < 300:
                    # Auth, throttling and temporary unavailable resources may recover.
                    permanent = response.status_code in {400, 413, 422}
                    raise InboundError(f"resend_http_{response.status_code}", permanent=permanent)
                result = json.loads(read_bounded(response, MAX_MAIL_BYTES))
                if not isinstance(result, dict):
                    raise InboundError("invalid_resend_response")
                return result
        except (requests.RequestException, ValueError):
            raise InboundError("resend_request_failed") from None

    def pages(self, path: str):
        cursor = None
        seen = set()
        while True:
            params = {"limit": 100}
            if cursor:
                params["after"] = cursor
            page = self.request("GET", path + "?" + urlencode(params))
            items = page.get("data", [])
            yield items
            if not page.get("has_more"):
                break
            if not items or items[-1]["id"] in seen:
                raise InboundError("invalid_resend_pagination")
            cursor = items[-1]["id"]
            seen.add(cursor)


def read_bounded(response, limit: int) -> bytes:
    data = bytearray()
    for chunk in response.iter_content(65536):
        if len(data) + len(chunk) > limit:
            raise InboundError("mail_size_limit", permanent=True)
        data.extend(chunk)
    return bytes(data)


def download_attachment(url: str, limit: int) -> bytes:
    # URLs come exclusively from the authenticated Resend API. Do not follow
    # redirects or forward the API credential to a CDN or arbitrary message URL.
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not host.endswith(".resend.com")
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise InboundError("attachment_host_rejected", permanent=True)
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(
            not ipaddress.ip_address(item[4][0]).is_global for item in addresses
        ):
            raise InboundError("attachment_address_rejected", permanent=True)
        with requests.get(url, timeout=(10, 45), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise InboundError("attachment_download_failed")
            return read_bounded(response, limit)
    except (OSError, requests.RequestException):
        raise InboundError("attachment_download_failed") from None


def enqueue_received(catalog: Catalog, email_id: str) -> bool:
    email_id = str(UUID(email_id))
    with catalog.connect() as conn:
        return bool(
            conn.execute(
                "INSERT INTO genchi_private.inbound_mail(email_id) VALUES(%s) ON CONFLICT DO NOTHING",
                (email_id,),
            ).rowcount
        )


def backfill(catalog: Catalog) -> dict:
    queued = 0
    for page in Resend().pages("/emails/receiving"):
        for item in page:
            queued += enqueue_received(catalog, item["id"])
    return {"queued": queued}


def reconcile(catalog: Catalog, client=None) -> int:
    """Recover missed webhooks using a checkpoint advanced only after a complete scan."""
    if not os.environ.get("RESEND_API_KEY") or not os.environ.get("RESEND_WEBHOOK_SECRET"):
        return 0
    client = client or Resend()
    with catalog.connect() as conn:
        conn.autocommit = True
        if not conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(current_schema() || ':inbound-poll',0)) AS locked"
        ).fetchone()["locked"]:
            return 0
        state = conn.execute(
            "SELECT last_email_id FROM genchi_private.inbound_cursor WHERE id=TRUE"
        ).fetchone()
        previous = str(state["last_email_id"]) if state and state["last_email_id"] else None
        newest = None
        queued = 0
        for page in client.pages("/emails/receiving"):
            done = False
            for item in page:
                email_id = str(UUID(item["id"]))
                newest = newest or email_id
                if email_id == previous:
                    done = True
                    break
                queued += enqueue_received(catalog, email_id)
            if done:
                break
        if newest:
            conn.execute(
                "INSERT INTO genchi_private.inbound_cursor(id,last_email_id) VALUES(TRUE,%s) ON CONFLICT(id) DO UPDATE SET last_email_id=EXCLUDED.last_email_id",
                (newest,),
            )
        return queued


def loop_token(email_id: str) -> str:
    signature = hmac.new(signing_key(), f"inbound:{email_id}".encode(), hashlib.sha256).hexdigest()
    return f"{email_id}.{signature}"


def is_forwarded(headers: dict) -> bool:
    value = next((v for k, v in headers.items() if k.lower() == "x-genchi-forwarded"), "")
    if not isinstance(value, str) or "." not in value:
        return False
    return hmac.compare_digest(value.encode(), loop_token(value.partition(".")[0]).encode())


def build_forward(client: Resend, email_id: str) -> dict | None:
    email = client.request("GET", f"/emails/receiving/{email_id}?html_format=cid")
    if is_forwarded(email.get("headers") or {}):
        return None
    recipient = os.environ.get("ADMIN_EMAIL", "").strip()
    sender = os.environ.get("MAIL_FROM", "").strip()
    if (
        not recipient
        or parseaddr(recipient)[1] != recipient
        or "@" not in recipient
        or any(c in recipient + sender for c in "\r\n")
        or not parseaddr(sender)[1]
    ):
        raise InboundError("invalid_forward_configuration")
    original_from = str(email.get("from") or "")
    # Keep original recipients in the body; forwarding always has exactly one destination.
    summary = (
        "Genchi 收件转发\n"
        f"发件人：{original_from}\n"
        f"原收件人：{', '.join(email.get('to') or [])}\n"
        f"时间：{email.get('created_at') or ''}\n"
        f"主题：{email.get('subject') or '（无主题）'}\n"
    )
    payload = {
        "from": sender,
        "to": [recipient],
        "subject": "[Genchi 收件] " + str(email.get("subject") or "（无主题）"),
        "headers": {"X-Genchi-Forwarded": loop_token(email_id), "Auto-Submitted": "auto-generated"},
    }
    reply = email.get("reply_to") or [original_from]
    if isinstance(reply, str):
        reply = [reply]
    addresses = [
        address
        for _, address in getaddresses(reply)
        if "@" in address and not any(c in address for c in "\r\n")
    ]
    if addresses:
        payload["reply_to"] = addresses
    if email.get("text") or not email.get("html"):
        payload["text"] = summary + "\n" + (email.get("text") or "")
    if email.get("html"):
        payload["html"] = "<pre>" + html.escape(summary) + "</pre><hr>" + email["html"]
    remaining = MAX_MAIL_BYTES - len(json.dumps(payload).encode())
    attachments = []
    for page in client.pages(f"/emails/receiving/{email_id}/attachments"):
        for item in page:
            limit = max(0, (remaining - 1024) * 3 // 4)
            if int(item.get("size") or 0) > limit:
                raise InboundError("mail_size_limit", permanent=True)
            content = download_attachment(item["download_url"], limit)
            attachment = {
                "filename": item.get("filename") or "attachment",
                "content": base64.b64encode(content).decode(),
            }
            if item.get("content_type"):
                attachment["content_type"] = item["content_type"]
            if item.get("content_id"):
                attachment["content_id"] = item["content_id"]
            remaining -= len(json.dumps(attachment).encode()) + 1
            if remaining < 0:
                raise InboundError("mail_size_limit", permanent=True)
            attachments.append(attachment)
    if attachments:
        payload["attachments"] = attachments
    if len(json.dumps(payload).encode()) > MAX_MAIL_BYTES:
        raise InboundError("mail_size_limit", permanent=True)
    return payload


def forward_one(catalog: Catalog, client=None) -> bool:
    if not os.environ.get("RESEND_API_KEY") or not os.environ.get("RESEND_WEBHOOK_SECRET"):
        return False
    client = client or Resend()
    # A session lock serializes sends without holding a DB transaction over HTTP.
    # Process death releases it; the frozen payload + Resend key make retry safe.
    with catalog.connect() as conn:
        conn.autocommit = True
        if not conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(current_schema() || ':inbound',0)) AS locked"
        ).fetchone()["locked"]:
            return False
        row = conn.execute("""SELECT * FROM genchi_private.inbound_mail
            WHERE status='PENDING' AND available_at<=NOW() ORDER BY available_at LIMIT 1""").fetchone()
        if not row:
            return False
        email_id = str(row["email_id"])
        if row["first_send_at"] and row["first_send_at"] < datetime.now(UTC) - timedelta(hours=23):
            conn.execute(
                "UPDATE genchi_private.inbound_mail SET status='UNCERTAIN',last_error='idempotency_window_expired' WHERE email_id=%s",
                (email_id,),
            )
            LOG.error("Inbound %s requires delivery verification", email_id)
            return True
        try:
            payload = row["payload"]
            if payload is None:
                payload = build_forward(client, email_id)
                if payload is None:
                    conn.execute(
                        "UPDATE genchi_private.inbound_mail SET status='SKIPPED',last_error='forwarding_loop' WHERE email_id=%s",
                        (email_id,),
                    )
                    return True
                payload = conn.execute(
                    "UPDATE genchi_private.inbound_mail SET payload=%s WHERE email_id=%s RETURNING payload",
                    (Jsonb(payload), email_id),
                ).fetchone()["payload"]
            conn.execute(
                """UPDATE genchi_private.inbound_mail SET attempts=attempts+1,
                first_send_at=COALESCE(first_send_at,NOW()) WHERE email_id=%s""",
                (email_id,),
            )
            result = client.request(
                "POST", "/emails", payload=payload, idempotency_key=f"genchi-inbound/{email_id}"
            )
            forwarded_id = str(UUID(result["id"]))
            conn.execute(
                """UPDATE genchi_private.inbound_mail SET status='SENT',sent_at=NOW(),
                forwarded_id=%s,payload=NULL,last_error=NULL WHERE email_id=%s""",
                (forwarded_id, email_id),
            )
            LOG.info("Inbound %s forwarded as %s", email_id, forwarded_id)
        except (InboundError, KeyError, ValueError) as exc:
            permanent = isinstance(exc, InboundError) and exc.permanent
            code = str(exc) if isinstance(exc, InboundError) else "invalid_resend_response"
            delay = min(3600, 30 * 2 ** min(row["attempts"], 7))
            conn.execute(
                """UPDATE genchi_private.inbound_mail SET status=%s,last_error=%s,
                available_at=NOW()+(%s * INTERVAL '1 second') WHERE email_id=%s""",
                ("FAILED" if permanent else "PENDING", code, delay, email_id),
            )
            LOG.warning("Inbound %s: %s", email_id, code)
        return True
