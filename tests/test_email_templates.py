"""Login mail readability, MIME fallback and untrusted rendering inputs."""

from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from unittest.mock import patch

import pytest
from genchi_product.emails import render_login_email, render_notification_email
from genchi_product.notifications import grouped_changes, localized_change, smtp_send


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.links = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "a":
            self.links.append(dict(attrs)["href"])

    def handle_data(self, data):
        self.text.append(data)


def test_template_retains_leading_zeroes_and_matching_readable_content():
    message = render_login_email("004281", expires_minutes=15, site_url="https://example.test/")
    parsed = Markup()
    parsed.feed(message.html)
    text = "".join(parsed.text)
    for value in ["004281", "15 分钟内有效", "下一次心动，", "现场见。", "请勿将验证码分享给他人"]:
        assert value in text and value in message.text
    assert parsed.links == ["https://example.test/zh-Hans"]
    assert not set(parsed.tags) & {"script", "img", "iframe", "form", "link"}
    assert "004281" not in repr(message) and "004281" not in message.subject


@pytest.mark.parametrize("code", ["12345", "1234567", "１２３４５６", "123456\n", "<svg/>"])
def test_invalid_or_injected_codes_cannot_be_rendered(code):
    with pytest.raises(ValueError, match="six-digit"):
        render_login_email(code, expires_minutes=15, site_url="https://example.test")


@pytest.mark.parametrize(
    "site",
    [
        "javascript:alert(1)",
        "//example.test",
        "https://name:secret@example.test",
        "https://example.test/path",
        "https://example.test?token=secret",
        "https://example.test#fragment",
        'https://example.test"onclick="alert(1)',
        "https://example.test\r\n",
    ],
)
def test_links_must_use_a_safe_configured_origin(site):
    with pytest.raises(ValueError, match="public origin"):
        render_login_email("004281", expires_minutes=15, site_url=site)


@pytest.mark.parametrize("minutes", [0, -1, True, 1.5, 61, "15"])
def test_invalid_expiry_cannot_be_misrepresented(minutes):
    with pytest.raises(ValueError, match="lifetime"):
        render_login_email("004281", expires_minutes=minutes, site_url="https://example.test")


def test_smtp_sends_html_with_plain_text_fallback_without_changing_reminders(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "mailpit")
    monkeypatch.setenv("SMTP_PORT", "1025")
    monkeypatch.setenv("SMTP_SECURITY", "none")
    monkeypatch.delenv("SMTP_USER", raising=False)
    rendered = render_login_email("004281", expires_minutes=15, site_url="http://localhost:13000")
    with patch("genchi_product.notifications.smtplib.SMTP") as smtp:
        connection = smtp.return_value.__enter__.return_value
        connection.send_message.return_value = {}
        smtp_send(
            "preview@example.test", rendered.subject, rendered.text, "preview", None,
            html=rendered.html,
        )
        sent = connection.send_message.call_args.args[0]
        decoded = BytesParser(policy=policy.default).parsebytes(sent.as_bytes())
        assert decoded.get_content_type() == "multipart/alternative"
        assert [part.get_content_type() for part in decoded.iter_parts()] == ["text/plain", "text/html"]
        assert "004281" in decoded.get_body(preferencelist=("plain",)).get_content()
        assert "004281" in decoded.get_body(preferencelist=("html",)).get_content()
        assert decoded["List-Unsubscribe"] is None
        smtp_send("preview@example.test", "Reminder", "Existing plain text", "reminder", None)
        reminder = connection.send_message.call_args.args[0]
        assert reminder.get_content_type() == "text/plain"
        assert reminder.get_content().strip() == "Existing plain text"


def test_notification_template_is_branded_compact_and_escapes_dynamic_content():
    rendered = render_notification_email(
        "Activity update",
        locale="en",
        site_url="https://example.test",
        eyebrow="Event information updated",
        heading='<script>alert("title")</script>',
        intro="Repeated entries have been grouped.",
        items=[("Doors open (14 entries)", None), ("Event starts", "https://example.test/en/a/1")],
        facts=[("Updated start time", "2026-09-23 15:30 JST")],
        cta_label="Event details and official sources",
        cta_url="https://example.test/en/activities/1",
        official_url="https://official.example.test/event",
        unsubscribe_url="https://example.test/en/unsubscribe?token=test-only",
    )
    parsed = Markup()
    parsed.feed(rendered.html)
    visible = "".join(parsed.text)
    assert '<script>alert("title")</script>' in visible
    assert "<script>" not in rendered.html
    assert "Doors open (14 entries)" in rendered.text and "Doors open (14 entries)" in visible
    assert "2026-09-23 15:30 JST" in rendered.text and "2026-09-23 15:30 JST" in visible
    assert "https://example.test/en/activities/1" in parsed.links
    assert not set(parsed.tags) & {"script", "img", "iframe", "form", "link"}


def test_repeated_collection_changes_are_grouped_before_rendering():
    changes = [
        *({"summary": "更新：活动开始"} for _ in range(14)),
        *({"summary": "更新：开放入场"} for _ in range(14)),
    ]
    assert grouped_changes(changes) == [("更新：活动开始", 14), ("更新：开放入场", 14)]
    assert localized_change("更新：开放入场", 14, "zh-Hans") == "更新：开放入场（14 项）"
    assert localized_change("更新：开放入场", 14, "en") == "Updated: Doors open (14 entries)"
    assert localized_change("更新：开放入场", 14, "ja") == "更新：開場（14 件）"
