from __future__ import annotations

import base64
import copy
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from genchi_product import inbound
from genchi_product.api import create_app
from svix.webhooks import Webhook
from test_product import catalog as catalog

SECRET = "whsec_" + base64.b64encode(b"test-inbound-secret-with-32-bytes!").decode()


@pytest.fixture(autouse=True)
def mail_env(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_only")
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("MAIL_FROM", "Genchi <hello@example.test>")
    monkeypatch.setenv("PRODUCT_SECRET", "test-product-secret-with-at-least-32-characters")


def signed(payload, *, age=0):
    raw = json.dumps(payload, ensure_ascii=False)
    now = datetime.now(UTC) - timedelta(seconds=age)
    message_id = "msg_test_delivery"
    return raw.encode(), {
        "svix-id": message_id,
        "svix-timestamp": str(int(now.timestamp())),
        "svix-signature": Webhook(SECRET).sign(message_id, now, raw),
        "Content-Type": "application/json",
    }


def test_webhook_authentication_durable_ack_and_duplicates(catalog, monkeypatch):
    client = TestClient(create_app(catalog))
    email_id = str(uuid.uuid4())
    payload = {"type": "email.received", "data": {"email_id": email_id, "subject": "日本語"}}
    raw, headers = signed(payload)
    assert client.post("/webhooks/resend", content=raw).status_code == 401
    assert client.post("/webhooks/resend", content=raw + b" ", headers=headers).status_code == 401
    stale, stale_headers = signed(payload, age=600)
    assert client.post("/webhooks/resend", content=stale, headers=stale_headers).status_code == 401
    assert client.post("/webhooks/resend", content=raw, headers=headers).json() == {
        "ok": True,
        "queued": True,
    }
    assert client.post("/webhooks/resend", content=raw, headers=headers).json() == {
        "ok": True,
        "queued": False,
    }
    with catalog.connect() as conn:
        row = conn.execute("SELECT * FROM genchi_private.inbound_mail").fetchone()
        assert str(row["email_id"]) == email_id
        assert row["status"] == "PENDING" and row["payload"] is None
    monkeypatch.delenv("RESEND_WEBHOOK_SECRET")
    assert client.post("/webhooks/resend", content=raw, headers=headers).status_code == 503


def test_webhook_rejects_bad_ids_large_bodies_and_never_acks_database_failure(catalog, monkeypatch):
    client = TestClient(create_app(catalog), raise_server_exceptions=False)
    raw, headers = signed({"type": "email.received", "data": {"email_id": "../../internal"}})
    assert client.post("/webhooks/resend", content=raw, headers=headers).status_code == 400
    assert client.post("/webhooks/resend", content=b"x" * 65537).status_code == 413
    raw, headers = signed({"type": "email.sent", "data": {}})
    assert client.post("/webhooks/resend", content=raw, headers=headers).json()["ignored"]

    def unavailable(*_):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("genchi_product.api.enqueue_received", unavailable)
    raw, headers = signed({"type": "email.received", "data": {"email_id": str(uuid.uuid4())}})
    assert client.post("/webhooks/resend", content=raw, headers=headers).status_code == 500


class FakeResend:
    def __init__(self, *, fail=False, headers=None):
        self.sent = []
        self.fail = fail
        self.headers = headers or {}

    def request(self, method, path, **kwargs):
        if method == "GET":
            assert path.endswith("?html_format=cid")
            return {
                "from": "Original <sender@example.test>",
                "to": ["contact@example.test"],
                "cc": ["nobody@example.test"],
                "bcc": ["hidden@example.test"],
                "subject": "原邮件",
                "text": "Original body",
                "html": '<p>原文</p><img src="cid:logo">',
                "headers": self.headers,
                "reply_to": ["reply@example.test"],
            }
        self.sent.append(copy.deepcopy(kwargs))
        if self.fail:
            raise inbound.InboundError("resend_request_failed")
        return {"id": str(uuid.uuid4())}

    def pages(self, path):
        assert path.endswith("/attachments")
        yield [
            {
                "filename": "logo.png",
                "content_id": "<logo>",
                "content_type": "image/png",
                "size": 3,
                "download_url": "https://inbound-cdn.resend.com/signed",
            }
        ]
        yield [
            {
                "filename": "notes.txt",
                "size": 3,
                "download_url": "https://inbound-cdn.resend.com/signed2",
            }
        ]


def test_forward_keeps_body_replies_and_all_attachments_without_cc(catalog, monkeypatch):
    monkeypatch.setattr(inbound, "download_attachment", lambda *_: b"abc")
    email_id = str(uuid.uuid4())
    inbound.enqueue_received(catalog, email_id)
    fake = FakeResend()
    assert inbound.forward_one(catalog, fake)
    request = fake.sent[0]
    assert request["idempotency_key"] == f"genchi-inbound/{email_id}"
    message = request["payload"]
    assert message["to"] == ["admin@example.test"]
    assert "cc" not in message and "bcc" not in message
    assert message["reply_to"] == ["reply@example.test"]
    assert "sender@example.test" in message["text"] and "contact@example.test" in message["text"]
    assert message["text"].endswith("Original body")
    assert 'src="cid:logo"' in message["html"]
    assert len(message["attachments"]) == 2
    assert message["attachments"][0]["content_id"] == "logo"
    assert base64.b64decode(message["attachments"][1]["content"]) == b"abc"
    assert not inbound.forward_one(catalog, fake)
    assert not inbound.enqueue_received(catalog, email_id)
    with catalog.connect() as conn:
        row = conn.execute("SELECT * FROM genchi_private.inbound_mail").fetchone()
        assert row["status"] == "SENT" and row["payload"] is None and row["forwarded_id"]


def test_uncertain_request_retries_exact_frozen_payload_and_key(catalog, monkeypatch):
    monkeypatch.setattr(inbound, "download_attachment", lambda *_: b"abc")
    email_id = str(uuid.uuid4())
    inbound.enqueue_received(catalog, email_id)
    fake = FakeResend(fail=True)
    inbound.forward_one(catalog, fake)
    first = copy.deepcopy(fake.sent[0])
    monkeypatch.setenv("ADMIN_EMAIL", "changed@example.test")

    def forbid_rebuild(*_):
        pytest.fail("Retry must use the saved payload, including its original destination")

    monkeypatch.setattr(inbound, "build_forward", forbid_rebuild)
    with catalog.connect() as conn:
        conn.execute("UPDATE genchi_private.inbound_mail SET available_at=NOW()")
    fake.fail = False
    inbound.forward_one(catalog, fake)
    assert fake.sent[-1] == first


def test_expired_idempotency_window_never_resends(catalog):
    inbound.enqueue_received(catalog, str(uuid.uuid4()))
    with catalog.connect() as conn:
        conn.execute(
            "UPDATE genchi_private.inbound_mail SET first_send_at=NOW()-INTERVAL '24 hours'"
        )
    fake = FakeResend()
    assert inbound.forward_one(catalog, fake)
    assert fake.sent == []
    with catalog.connect() as conn:
        assert (
            conn.execute("SELECT status FROM genchi_private.inbound_mail").fetchone()["status"]
            == "UNCERTAIN"
        )


def test_signed_forward_loop_is_skipped_but_forged_marker_is_not(catalog):
    inbound.enqueue_received(catalog, str(uuid.uuid4()))
    fake = FakeResend(headers={"X-Genchi-Forwarded": inbound.loop_token(str(uuid.uuid4()))})
    assert inbound.forward_one(catalog, fake)
    assert fake.sent == []
    assert not inbound.is_forwarded({"X-Genchi-Forwarded": "fake.signature"})
    assert not inbound.is_forwarded({"X-Genchi-Forwarded": "伪造.签名"})
    with catalog.connect() as conn:
        assert (
            conn.execute("SELECT status FROM genchi_private.inbound_mail").fetchone()["status"]
            == "SKIPPED"
        )


def test_attachment_failure_never_sends_incomplete_mail(catalog, monkeypatch):
    def fail(*_):
        raise inbound.InboundError("mail_size_limit", permanent=True)

    monkeypatch.setattr(inbound, "download_attachment", fail)
    inbound.enqueue_received(catalog, str(uuid.uuid4()))
    fake = FakeResend()
    inbound.forward_one(catalog, fake)
    assert fake.sent == []
    with catalog.connect() as conn:
        row = conn.execute("SELECT * FROM genchi_private.inbound_mail").fetchone()
        assert row["status"] == "FAILED" and row["last_error"] == "mail_size_limit"
        assert row["first_send_at"] is None


def test_concurrent_forwarder_does_not_send_twice(catalog):
    inbound.enqueue_received(catalog, str(uuid.uuid4()))
    fake = FakeResend()
    with catalog.connect() as lock:
        lock.execute("SELECT pg_advisory_lock(hashtextextended(current_schema() || ':inbound',0))")
        assert not inbound.forward_one(catalog, fake)
        assert not fake.sent


@pytest.mark.parametrize(
    "url",
    [
        "http://inbound-cdn.resend.com/file",
        "https://localhost/file",
        "https://inbound-cdn.resend.com.evil.test/file",
        "https://secret@inbound-cdn.resend.com/file",
    ],
)
def test_attachment_urls_cannot_reach_arbitrary_hosts(url):
    with pytest.raises(inbound.InboundError, match="attachment_host_rejected"):
        inbound.download_attachment(url, 100)


def test_attachment_url_public_address_and_size_bounds(monkeypatch):
    monkeypatch.setattr(
        inbound.socket,
        "getaddrinfo",
        lambda *_args, **_kw: [(None, None, None, None, ("127.0.0.1", 443))],
    )
    with pytest.raises(inbound.InboundError, match="attachment_address_rejected"):
        inbound.download_attachment("https://inbound-cdn.resend.com/file", 100)

    class LargeResponse:
        def iter_content(self, *_):
            yield b"123"
            yield b"456"

    with pytest.raises(inbound.InboundError, match="mail_size_limit"):
        inbound.read_bounded(LargeResponse(), 5)


def test_resend_current_cdn_is_allowed_without_credentials_or_redirects(monkeypatch):
    monkeypatch.setattr(
        inbound.socket,
        "getaddrinfo",
        lambda *_args, **_kw: [(None, None, None, None, ("8.8.8.8", 443))],
    )

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def iter_content(self, *_):
            yield b"attachment"

    def get(url, **kwargs):
        assert url == "https://cdn.resend.app/file"
        assert kwargs["allow_redirects"] is False
        assert "headers" not in kwargs
        return Response()

    monkeypatch.setattr(inbound.requests, "get", get)
    assert inbound.download_attachment("https://cdn.resend.app/file", 100) == b"attachment"


def test_resend_pagination_uses_cursor_and_detects_repeated_pages(monkeypatch):
    client = inbound.Resend()
    calls = []

    def respond(method, path):
        calls.append(path)
        return {"data": [{"id": "cursor1"}], "has_more": len(calls) == 1}

    monkeypatch.setattr(client, "request", respond)
    assert len(list(client.pages("/emails/receiving"))) == 2
    assert calls[-1].endswith("limit=100&after=cursor1")


def test_reconciliation_recovers_partial_scan_without_skipping_mail(catalog):
    old, first, second = [str(uuid.uuid4()) for _ in range(3)]
    with catalog.connect() as conn:
        conn.execute("INSERT INTO genchi_private.inbound_cursor VALUES(TRUE,%s)", (old,))

    class Inbox:
        fail = True

        def pages(self, path):
            assert path == "/emails/receiving"
            yield [{"id": first}]
            if self.fail:
                raise inbound.InboundError("resend_request_failed")
            yield [{"id": second}, {"id": old}]

    inbox = Inbox()
    with pytest.raises(inbound.InboundError):
        inbound.reconcile(catalog, inbox)
    with catalog.connect() as conn:
        assert (
            str(
                conn.execute("SELECT last_email_id FROM genchi_private.inbound_cursor").fetchone()[
                    "last_email_id"
                ]
            )
            == old
        )
    inbox.fail = False
    assert inbound.reconcile(catalog, inbox) == 1
    assert inbound.reconcile(catalog, inbox) == 0
    with catalog.connect() as conn:
        assert (
            str(
                conn.execute("SELECT last_email_id FROM genchi_private.inbound_cursor").fetchone()[
                    "last_email_id"
                ]
            )
            == first
        )
        rows = conn.execute("SELECT email_id FROM genchi_private.inbound_mail").fetchall()
        assert {str(row["email_id"]) for row in rows} == {first, second}
