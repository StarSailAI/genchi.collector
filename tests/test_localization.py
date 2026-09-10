"""Locale isolation, original-source preservation and passwordless mail language."""

import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from genchi_product.api import create_app
from genchi_product.emails import render_login_email
from genchi_product.localization import LOCALES, localized_catalog, translate
from genchi_product.notifications import render_mail
from test_product import NOW, activity, subscribe
from test_product import catalog as product_catalog

catalog = product_catalog


@pytest.mark.parametrize("locale", LOCALES)
def test_mail_has_matching_language_text_and_html(locale):
    message = render_login_email(
        "004281", expires_minutes=15, site_url="https://example.test", locale=locale
    )
    assert f'lang="{locale}"' in message.html
    assert f"https://example.test/{locale}" in message.html
    assert "004281" in message.text and "004281" in message.html
    assert translate("你的登录验证码", locale) in message.text
    assert translate("你的登录验证码", locale) in message.html
    assert "$" not in message.html and "{minutes}" not in message.html
    assert "004281" not in message.subject and "004281" not in repr(message)
    if locale in {"en", "ja"}:
        assert "验证码" not in message.html


def test_unknown_mail_locale_falls_back_explicitly():
    expected = render_login_email("004281", expires_minutes=15, site_url="https://example.test")
    assert (
        render_login_email(
            "004281", expires_minutes=15, site_url="https://example.test", locale='"<script>'
        )
        == expected
    )


def test_projection_preserves_identifiers_and_official_source_text():
    source = {
        "id": "123",
        "title": "学園アイドルマスター",
        "title_zh": "学园偶像大师",
        "evidence": [{"excerpt": "原始证据"}],
        "summary": "活动说明",
    }
    traditional = localized_catalog(source, "zh-Hant")
    assert traditional["title_localized"] == "學園偶像大師"
    assert traditional["title"] == source["title"] and traditional["title_zh"] == source["title_zh"]
    assert traditional["evidence"] == source["evidence"]
    assert traditional["summary_localized"] == "活動說明"
    assert "title_localized" not in source
    for locale in ("ja", "en"):
        assert localized_catalog(source, locale)["title_localized"] == source["title"]


@pytest.mark.parametrize("locale", LOCALES)
def test_language_survives_signup_profile_and_reminder(catalog, monkeypatch, locale):
    sent = []
    monkeypatch.setattr(
        "genchi_product.auth.smtp_send", lambda *args, **kw: sent.append((args, kw))
    )
    client = TestClient(
        create_app(catalog), headers={"Origin": "http://localhost:13000", "X-Genchi-Locale": locale}
    )
    email = "locale@example.test"
    response = client.post("/auth/login", json={"email": email, "locale": locale})
    assert response.status_code == 200, response.text
    assert f'lang="{locale}"' in sent[0][1]["html"]
    code = re.search(r"\b[0-9]{6}\b", sent[0][0][2])[0]
    assert (
        client.post(
            "/auth/verify", json={"email": email, "code": code, "locale": locale}
        ).status_code
        == 200
    )
    me = client.get("/me").json()
    assert me["locale"] == locale
    next_locale = "ja" if locale != "ja" else "en"
    assert (
        client.put(
            "/me",
            json={
                "display_name": "QA",
                "timezone": "Asia/Tokyo",
                "unsubscribed": True,
                "locale": next_locale,
            },
        ).status_code
        == 200
    )
    assert client.get("/me").json()["locale"] == next_locale
    # Updating other profile settings doesn't reset language or silently re-enable mail.
    assert (
        client.put(
            "/me", json={"display_name": "QA", "timezone": "Asia/Tokyo", "unsubscribed": True}
        ).status_code
        == 200
    )
    assert client.get("/me").json()["locale"] == next_locale
    assert client.get("/me").json()["unsubscribed"] is True
    assert (
        client.post("/auth/login", json={"email": email, "locale": "not-a-language"}).status_code
        == 422
    )


def test_concurrent_requests_do_not_leak_language(catalog):
    app = create_app(catalog)

    def read(locale):
        c = TestClient(app)
        response = c.get("/me", headers={"X-Genchi-Locale": locale})
        return locale, response

    with ThreadPoolExecutor(max_workers=4) as pool:
        for locale, response in pool.map(read, LOCALES * 3):
            assert response.status_code == 401
            assert response.json()["detail"] == translate("请先验证邮箱并登录", locale)
            assert response.headers["Content-Language"] == locale


def test_catalog_and_reminder_names_follow_locale_without_rewriting_source(catalog):
    item = activity()
    aid = catalog.publish(item)
    subscribe(catalog, aid)
    c = TestClient(create_app(catalog))
    for locale in LOCALES:
        response = c.get("/activities/" + aid, headers={"X-Genchi-Locale": locale})
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["title"] == item.title
        assert value["title_localized"]
        if locale in {"en", "ja"}:
            assert value["title_localized"] == item.title
        with catalog.connect() as conn:
            user = conn.execute(
                "SELECT * FROM genchi_private.accounts WHERE id='user-one'"
            ).fetchone()
            user["locale"] = locale
            result = render_mail(
                conn, {"kind": "DIGEST", "payload": {"activity_ids": [aid]}}, user, NOW
            )
            assert result and result[0] == translate("你关注的新活动 · Genchi", locale)
            assert f"/{locale}/activities/{aid}" in result[1]
            assert f"/{locale}/dashboard" in result[1]
