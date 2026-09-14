"""Transactional authentication acceptance. SMTP is always an in-memory stub."""

import hashlib
import hmac
import re
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from genchi_product.api import create_app
from genchi_product.auth import digest, normalize_email, rate_limit
from genchi_product.notifications import eligible_follows
from genchi_product.presentation import followed_ids
from test_product import activity
from test_product import catalog as product_catalog

catalog = product_catalog

ORIGIN = "http://localhost:13000"


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr("genchi_product.auth.smtp_send", lambda *a, **kw: sent.append(a))
    return sent


def client(catalog):
    return TestClient(create_app(catalog), headers={"Origin": ORIGIN})


def send(c, mail, email="person@example.test"):
    r = c.post("/auth/login", json={"email": email})
    assert r.status_code == 200, r.text
    assert r.json()["expires_in"] == 900
    assert r.json()["auth_method"] == "email_code"
    return re.search(r"\b[0-9]{6}\b", mail[-1][2]).group(0)


def verify(c, code, email="person@example.test"):
    return c.post("/auth/verify", json={"email": email, "code": code})


def release_limits(catalog):
    with catalog.connect() as conn:
        conn.execute("UPDATE genchi_private.auth_limits SET window_start=NOW()-INTERVAL '1 day'")


def test_normalization():
    assert normalize_email(" Person+tag@Example.TEST ") == "person+tag@example.test"
    for bad in (None, {}, "bad", "a..b@example.test", "a@-example.test", "a\n@example.test"):
        with pytest.raises(ValueError):
            normalize_email(bad)


def test_codes_hashed_single_use_and_cookie(catalog, mail):
    c = client(catalog)
    assert c.get("/health").json()["auth_method"] == "email_code"
    code = send(c, mail, "Person@Example.TEST")
    with catalog.connect() as conn:
        assert conn.execute("SELECT count(*) n FROM genchi_private.accounts").fetchone()["n"] == 0
        challenge = conn.execute("SELECT * FROM genchi_private.email_challenges").fetchone()
        assert code not in str(challenge)
        assert conn.execute("SELECT count(*) n FROM genchi_private.mail_queue").fetchone()["n"] == 0
    response = verify(c, code)
    assert response.status_code == 200
    assert response.json()["is_new_user"] is True
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=2592000" in cookie
    assert "Domain=" not in cookie
    assert response.headers["cache-control"] == "no-store"
    assert verify(c, code).status_code == 400
    me = c.get("/me").json()
    assert me["email"] == "person@example.test" and me["display_name"] == ""
    assert not me["is_admin"]
    with catalog.connect() as conn:
        row = conn.execute("SELECT * FROM genchi_private.sessions").fetchone()
        assert row["token_hash"] == digest(c.cookies["genchi_session"])


def test_wrong_attempts_commit_and_expire(catalog, mail):
    c = client(catalog)
    code = send(c, mail)
    wrong = "000000" if code != "000000" else "000001"
    for i in range(5):
        assert verify(c, wrong).status_code == 400
        with catalog.connect() as conn:
            assert (
                conn.execute("SELECT attempts FROM genchi_private.email_challenges").fetchone()[
                    "attempts"
                ]
                == i + 1
            )
    assert verify(c, code).status_code == 400
    release_limits(catalog)
    code = send(c, mail)
    with catalog.connect() as conn:
        conn.execute(
            "UPDATE genchi_private.email_challenges SET expires_at=NOW()-INTERVAL '1 second'"
        )
    assert verify(c, code).status_code == 400


def test_resend_invalidates_old_and_is_rate_limited(catalog, mail):
    c = client(catalog)
    old = send(c, mail)
    r = c.post("/auth/login", json={"email": "PERSON@example.test"})
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 60
    assert len(mail) == 1
    release_limits(catalog)
    new = send(c, mail)
    assert new != old
    assert verify(c, old).status_code == 400
    assert verify(c, new).status_code == 200


def test_smtp_failure_has_no_usable_code(catalog, monkeypatch):
    captured = []

    def fail(*args, **kwargs):
        captured.append(args)
        raise TimeoutError("private smtp credentials must not leak")

    monkeypatch.setattr("genchi_product.auth.smtp_send", fail)
    c = client(catalog)
    r = c.post("/auth/login", json={"email": "person@example.test"})
    assert r.status_code == 503 and "credentials" not in r.text
    code = re.search(r"\b[0-9]{6}\b", captured[0][2]).group(0)
    assert verify(c, code).status_code == 400
    with catalog.connect() as conn:
        assert (
            conn.execute("SELECT status FROM genchi_private.email_challenges").fetchone()["status"]
            == "FAILED"
        )
        assert conn.execute("SELECT count(*) n FROM genchi_private.accounts").fetchone()["n"] == 0


def test_sources_cannot_spoof_rate_limits(catalog, mail, monkeypatch):
    c = client(catalog)
    for i in range(5):
        r = c.post(
            "/auth/login",
            json={"email": f"user{i}@example.test"},
            headers={"X-Forwarded-For": f"1.2.3.{i}", "X-Real-IP": f"1.2.3.{i}"},
        )
        assert r.status_code == 200
    other_process = client(catalog)
    assert other_process.post("/auth/login", json={"email": "next@example.test"}).status_code == 429
    assert (
        c.post(
            "/auth/login",
            json={"email": "new@example.test"},
            headers={"X-Genchi-Client-IP": "8.8.8.8"},
        ).status_code
        == 403
    )
    key = "p" * 40
    monkeypatch.setenv("PRODUCT_PROXY_SECRET", key)
    stamp = str(int(time.time()))
    signature = hmac.new(
        key.encode(), f"{stamp}\nPOST\n/auth/login\n8.8.8.8".encode(), hashlib.sha256
    ).hexdigest()
    headers = {
        "X-Genchi-Client-IP": "8.8.8.8",
        "X-Genchi-Proxy-Time": stamp,
        "X-Genchi-Proxy-Signature": signature,
    }
    assert (
        c.post("/auth/login", json={"email": "new@example.test"}, headers=headers).status_code
        == 200
    )
    assert (
        c.post(
            "/auth/verify", json={"email": "new@example.test", "code": "000000"}, headers=headers
        ).status_code
        == 403
    )


def test_atomic_limits_and_single_concurrent_verification(catalog, mail):
    def hit(_):
        try:
            rate_limit(catalog, [("parallel-test", "one", 60, 3)])
            return 200
        except HTTPException as e:
            return e.status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(hit, range(12)))
    assert statuses.count(200) == 3 and statuses.count(429) == 9
    code = send(client(catalog), mail)
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(lambda _: verify(client(catalog), code).status_code, range(4)))
    assert statuses.count(200) == 1
    with catalog.connect() as conn:
        assert conn.execute("SELECT count(*) n FROM genchi_private.accounts").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) n FROM genchi_private.sessions").fetchone()["n"] == 1


def test_account_reuse_profile_ownership_session_revocation(catalog, mail):
    first, second = client(catalog), client(catalog)
    assert verify(first, send(first, mail)).status_code == 200
    own = first.get("/me").json()["id"]
    profile = {"display_name": "  星星  ", "timezone": "Asia/Tokyo", "unsubscribed": True}
    assert first.put("/me", json={**profile, "is_admin": True}).status_code == 422
    assert first.put("/me", json=profile).status_code == 200
    assert first.put("/me", json={**profile, "timezone": "invalid"}).status_code == 422
    release_limits(catalog)
    assert verify(second, send(second, mail)).json()["is_new_user"] is False
    assert second.get("/me").json()["id"] == own
    assert second.get("/me").json()["display_name"] == "星星"
    assert second.get("/me").json()["timezone"] == "Asia/Tokyo"
    assert second.get("/me").json()["unsubscribed"] is True
    assert first.post("/auth/logout").status_code == 200
    assert first.get("/me").status_code == 401 and second.get("/me").status_code == 200
    old = second.cookies["genchi_session"]
    assert second.post("/auth/logout-all").status_code == 200
    first.cookies.set("genchi_session", old)
    assert first.get("/me").status_code == 401
    release_limits(catalog)
    assert verify(first, send(first, mail)).status_code == 200
    with catalog.connect() as conn:
        conn.execute("UPDATE genchi_private.accounts SET disabled_at=NOW() WHERE id=%s", (own,))
    assert first.get("/me").status_code == 401


def test_registration_closed_csrf_and_validation(catalog, mail, monkeypatch):
    c = client(catalog)
    no_origin = TestClient(create_app(catalog))
    assert no_origin.post("/auth/login", json={"email": "person@example.test"}).status_code == 403
    assert (
        c.post(
            "/auth/login",
            json={"email": "person@example.test"},
            headers={"Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )
    assert c.post("/auth/login", json={"email": None}).status_code == 422
    r = c.post("/auth/verify", json={"email": "person@example.test", "code": "secret-long-invalid"})
    assert r.status_code == 422 and "secret-long-invalid" not in r.text
    monkeypatch.setenv("AUTH_REGISTRATION_OPEN", "false")
    assert c.post("/auth/login", json={"email": "person@example.test"}).status_code == 403
    assert not mail


def test_keyword_tag_follow_matching_and_privacy(catalog, mail):
    aid = catalog.publish(activity(), historical=True)
    c = client(catalog)
    verify(c, send(c, mail))
    user = c.get("/me").json()["id"]
    for kind, value in [("KEYWORD", "ＴＥＳＴ"), ("TAG", "LIVE")]:
        response = c.put("/me/follows", json={"target_type": kind, "target_id": value})
        assert response.status_code == 200, response.text
        fid = response.json()["id"]
        with catalog.connect() as conn:
            assert followed_ids(conn, user) == [aid]
            assert eligible_follows(conn, aid)[0]["account_id"] == user
        assert c.get("/me/activities").json()["total"] == 1
        assert c.delete("/me/follows/" + fid).status_code == 200
    assert c.put("/me/follows", json={"target_type": "TAG", "target_id": "evil"}).status_code == 422
    assert (
        c.put("/me/follows", json={"target_type": "KEYWORD", "target_id": "x"}).status_code == 422
    )
    for keyword in ("%_", "  test  ", "TEST"):
        assert (
            c.put("/me/follows", json={"target_type": "KEYWORD", "target_id": keyword}).status_code
            == 200
        )
    assert len(c.get("/me").json()["follows"]) == 2
    other = client(catalog)
    verify(other, send(other, mail, "other@example.test"), "other@example.test")
    fid = c.get("/me").json()["follows"][0]["id"]
    other.delete("/me/follows/" + fid)
    assert other.get("/me").json()["follows"] == []
    assert len(c.get("/me").json()["follows"]) == 2
    assert c.get("/tags").json()[0]["slug"] == "LIVE"


def test_agent_key_is_one_time_scoped_revocable_and_mcp_compatible(catalog, mail):
    c = client(catalog)
    verify(c, send(c, mail))
    created = c.post(
        "/me/api-keys",
        json={
            "name": "My Agent",
            "scopes": [
                "activities:read",
                "updates:read",
                "agenda:read",
                "subscriptions:read",
                "subscriptions:write",
            ],
        },
    )
    assert created.status_code == 201, created.text
    key = created.json()
    assert key["secret"].startswith("gch_live_")
    assert key["secret"] not in str(c.get("/me/api-keys").json())
    headers = {"Authorization": "Bearer " + key["secret"]}

    connection = c.get("/agent/v1/me", headers=headers)
    assert connection.status_code == 200
    assert connection.json()["name"] == "My Agent"
    assert connection.json()["prefix"] == key["prefix"]
    assert "secret" not in connection.text

    subscriptions = c.get("/agent/v1/subscriptions", headers=headers)
    assert subscriptions.status_code == 200 and subscriptions.json()["items"] == []
    added = c.post(
        "/agent/v1/subscriptions",
        headers=headers,
        json={"target_type": "SUBJECT", "target_id": "gakumas"},
    )
    assert added.status_code == 201
    first = c.get("/agent/v1/updates", headers=headers).json()
    assert first["next_cursor"]
    assert (
        c.get(
            "/agent/v1/updates", headers=headers, params={"cursor": first["next_cursor"]}
        ).status_code
        == 200
    )

    initialized = c.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert initialized.status_code == 200
    assert initialized.json()["result"]["serverInfo"]["name"] == "genchi"
    tools = c.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ).json()["result"]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    assert {"get_connection_info", "get_latest_updates"} <= set(by_name)
    subscription_schema = by_name["create_subscription"]["inputSchema"]["properties"]
    assert {"reminder_hours", "include_children", "kinds", "cities"} <= set(
        subscription_schema
    )

    assert c.delete("/me/api-keys/" + key["id"]).status_code == 200
    assert c.get("/agent/v1/subscriptions", headers=headers).status_code == 401
    with catalog.connect() as conn:
        stored = conn.execute(
            "SELECT secret_digest FROM genchi_private.api_keys WHERE id=%s", (key["id"],)
        ).fetchone()["secret_digest"]
        assert key["secret"] not in stored
        assert (
            conn.execute(
                "SELECT count(*) n FROM genchi_private.api_key_requests WHERE api_key_id=%s",
                (key["id"],),
            ).fetchone()["n"]
            >= 4
        )


def test_agent_key_scope_is_enforced(catalog, mail):
    c = client(catalog)
    verify(c, send(c, mail))
    key = c.post(
        "/me/api-keys",
        json={"name": "Read only", "scopes": ["activities:read"]},
    ).json()["secret"]
    headers = {"Authorization": "Bearer " + key}
    assert c.get("/agent/v1/activities", headers=headers).status_code == 200
    denied = c.get("/agent/v1/subscriptions", headers=headers)
    assert denied.status_code == 403 and "subscriptions:read" in denied.text


def test_agent_key_defaults_to_read_only(catalog, mail):
    c = client(catalog)
    verify(c, send(c, mail))
    created = c.post("/me/api-keys", json={"name": "Daily monitor"})
    assert created.status_code == 201
    key = created.json()
    assert set(key["scopes"]) == {
        "activities:read",
        "updates:read",
        "agenda:read",
        "subscriptions:read",
    }
    headers = {"Authorization": "Bearer " + key["secret"]}
    assert c.get("/agent/v1/subscriptions", headers=headers).status_code == 200
    denied = c.post(
        "/agent/v1/subscriptions",
        headers=headers,
        json={"target_type": "SUBJECT", "target_id": "gakumas"},
    )
    assert denied.status_code == 403 and "subscriptions:write" in denied.text


def test_agent_update_cursor_can_start_now_and_advance_without_matches(catalog, mail):
    c = client(catalog)
    verify(c, send(c, mail))
    key = c.post("/me/api-keys", json={"name": "Monitor"}).json()["secret"]
    headers = {"Authorization": "Bearer " + key}

    initial = c.get(
        "/agent/v1/updates", headers=headers, params={"cursor": "now"}
    ).json()
    assert initial["items"] == [] and initial["has_more"] is False

    catalog.publish(activity(key="upstream:after-monitor-start"))
    advanced = c.get(
        "/agent/v1/updates",
        headers=headers,
        params={"cursor": initial["next_cursor"], "mode": "following"},
    ).json()
    assert advanced["items"] == []
    assert advanced["next_cursor"] != initial["next_cursor"]

    unchanged = c.get(
        "/agent/v1/updates",
        headers=headers,
        params={"cursor": advanced["next_cursor"], "mode": "following"},
    ).json()
    assert unchanged["items"] == []
    assert unchanged["next_cursor"] == advanced["next_cursor"]

    historical = c.get(
        "/agent/v1/updates", headers=headers, params={"mode": "all"}
    ).json()
    assert historical["items"]
    assert c.get(
        "/agent/v1/updates", headers=headers, params={"cursor": "invalid"}
    ).status_code == 422


def test_https_cookie_session_expiry_and_all_devices(catalog, mail, monkeypatch):
    monkeypatch.setenv("PUBLIC_SITE_URL", "https://events.example.test")

    def secure_client():
        return TestClient(
            create_app(catalog),
            base_url="https://events.example.test",
            headers={"Origin": "https://events.example.test"},
        )

    a, b = secure_client(), secure_client()
    response = verify(a, send(a, mail))
    assert response.status_code == 200 and "Secure" in response.headers["set-cookie"]
    release_limits(catalog)
    assert verify(b, send(b, mail)).status_code == 200
    assert a.get("/me").status_code == b.get("/me").status_code == 200
    assert a.post("/auth/logout-all").status_code == 200
    assert b.get("/me").status_code == 401
    assert "Max-Age=0" in b.get("/me").headers.get("set-cookie", "") or not b.cookies
    release_limits(catalog)
    assert verify(a, send(a, mail)).status_code == 200
    with catalog.connect() as conn:
        conn.execute("UPDATE genchi_private.sessions SET expires_at=NOW()-INTERVAL '1 second'")
    assert a.get("/me").status_code == 401


@pytest.mark.parametrize("scope,maximum", [("send-global-hour", 100), ("send-global-day", 500)])
def test_global_mail_quotas(catalog, mail, scope, maximum):
    from genchi_product.notifications import signing_key

    key = hmac.new(signing_key(), f"rate:{scope}:all".encode(), hashlib.sha256).hexdigest()
    with catalog.connect() as conn:
        conn.execute(
            "INSERT INTO genchi_private.auth_limits(key,attempts) VALUES(%s,%s)", (key, maximum)
        )
    c = client(catalog)
    response = c.post("/auth/login", json={"email": "person@example.test"})
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
    assert not mail
