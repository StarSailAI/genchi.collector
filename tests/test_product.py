from __future__ import annotations

import json
import os
import re
import runpy
import ssl
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest
from fastapi.testclient import TestClient
from genchi_product.api import create_app, digest
from genchi_product.domain import ActivityInput, EvidenceInput, MilestoneInput, Moment, legacy_time
from genchi_product.notifications import (
    deliver_one,
    eligible_follows,
    plan,
    render_mail,
    smtp_send,
    unsubscribe_token,
)
from genchi_product.pipeline import extract_text, process_one
from genchi_product.store import Catalog
from psycopg.rows import dict_row
from pydantic import ValidationError

DSN = os.getenv("ALLFEEDS_TEST_DATABASE_URL")
NOW = datetime(2030, 6, 1, 0, tzinfo=UTC)


def test_resend_smtp_uses_verified_tls_and_stable_idempotency_key(monkeypatch):
    for key, value in {
        "SMTP_HOST": "smtp.resend.com",
        "SMTP_PORT": "465",
        "SMTP_SECURITY": "ssl",
        "SMTP_USER": "resend",
        "SMTP_PASSWORD": "re_test_only",
        "PRODUCT_SECRET": "s" * 40,
        "MAIL_FROM": "Genchi <hello@example.test>",
        "PUBLIC_SITE_URL": "https://events.example.test",
    }.items():
        monkeypatch.setenv(key, value)
    with patch("genchi_product.notifications.smtplib.SMTP_SSL") as smtp:
        smtp.return_value.__enter__.return_value.send_message.return_value = {}
        smtp_send("recipient@example.test", "A reminder", "Body", "stable-mail-id", "account-id")
    args, kwargs = smtp.call_args
    assert args == ("smtp.resend.com", 465)
    assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
    assert kwargs["context"].check_hostname is True
    connection = smtp.return_value.__enter__.return_value
    connection.login.assert_called_once_with("resend", "re_test_only")
    message = connection.send_message.call_args.args[0]
    assert message["Resend-Idempotency-Key"] == "stable-mail-id"
    assert "https://events.example.test/" in message["List-Unsubscribe"]


def proof(verified=True, version="one"):
    return EvidenceInput(
        excerpt="Official performance and ticket schedule",
        source_id="official",
        version_hash=version,
        verified=verified,
    )


def activity(key="upstream:1", start=None, verified=True):
    return ActivityInput(
        source_key=key,
        title="学園アイドルマスター TEST LIVE",
        kind="LIVE",
        subject_slugs=["gakumas"],
        publication="PUBLISHED",
        occurrence_key=key,
        venue="Tokyo Hall",
        evidence=proof(verified),
        time=Moment(precision="TIME", starts_at=start or NOW + timedelta(days=20)),
        milestones=[
            MilestoneInput(
                source_key="ticket:1",
                kind="TICKET",
                title="先行抽选",
                round_key="round:1",
                evidence=proof(verified),
                time=Moment(
                    precision="TIME",
                    starts_at=NOW + timedelta(hours=1),
                    ends_at=NOW + timedelta(days=3),
                ),
            )
        ],
    )


@pytest.fixture
def catalog(monkeypatch):
    if not DSN:
        pytest.skip("ALLFEEDS_TEST_DATABASE_URL is not set")
    schema = "product_test_" + uuid.uuid4().hex[:12]
    private = schema + "_private"
    monkeypatch.setenv("PRODUCT_SECRET", "test-only-secret-with-at-least-32-characters")
    monkeypatch.setenv("PUBLIC_SITE_URL", "http://localhost:13000")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    monkeypatch.delenv("PRODUCT_ADMIN_TOKEN", raising=False)
    statements = []
    with patch("alembic.op.execute", statements.append):
        runpy.run_path(str(Path("services/normalizer/alembic/versions/0004_catalog_v2.py")))[
            "upgrade"
        ]()
        runpy.run_path(str(Path("services/normalizer/alembic/versions/0006_catalog_names.py")))[
            "upgrade"
        ]()
        runpy.run_path(str(Path("services/normalizer/alembic/versions/0007_inbound_mail.py")))[
            "upgrade"
        ]()
        runpy.run_path(str(Path("services/normalizer/alembic/versions/0008_email_code_auth.py")))["upgrade"]()
        runpy.run_path(str(Path("services/normalizer/alembic/versions/0009_account_locale.py")))["upgrade"]()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with psycopg.connect(DSN, options=f"-c search_path={schema},public") as conn:
            conn.execute("""CREATE TABLE "Ip"(slug text,"nameJa" text,"nameZh" text,"colorHex" text);
                INSERT INTO "Ip" VALUES('idolmaster','アイドルマスター','偶像大师','#111111');
                CREATE TABLE "SchemaContract"(id int,major int,minor int,"updatedAt" timestamptz);
                INSERT INTO "SchemaContract" VALUES(1,1,1,NOW());
                CREATE TABLE resources(id bigserial PRIMARY KEY,source_id text,external_id text,content_hash text,kind text,
                  title text,content text,url text,attributes jsonb DEFAULT '{}',tags jsonb DEFAULT '[]',published_at timestamptz,observed_at timestamptz);
                CREATE TABLE "SearchDocument"("id" text,"entityType" text,"entityId" text,"titleOriginal" text,
                  "bodyOriginal" text,"projectKey" text,"kind" text,"country" text,"publishedAt" timestamptz,
                  "canonicalUrl" text,"searchText" text,"updatedAt" timestamptz, UNIQUE("entityType","entityId"));""")
            for statement in statements:
                conn.execute(
                    statement.replace("genchi_private", private)
                    .replace("genchi.", schema + ".")
                    .replace("allfeeds.", schema + ".")
                )

        class ScopedConnection(psycopg.Connection):
            def execute(self, query, params=None, **kwargs):
                if isinstance(query, str):
                    # Test schemas must not contend with the running local notification planner.
                    query = query.replace(
                        "pg_try_advisory_xact_lock(79321818)",
                        "pg_try_advisory_xact_lock(hashtextextended(current_schema(),0))",
                    )
                    query = query.replace("genchi_private.", private + ".").replace(
                        "allfeeds.", schema + "."
                    )
                return super().execute(query, params, **kwargs)

        class TestCatalog(Catalog):
            def connect(self):
                return ScopedConnection.connect(
                    DSN, row_factory=dict_row, options=f"-c search_path={schema},public"
                )

        yield TestCatalog(DSN)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{private}" CASCADE')
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def subscribe(catalog, activity_id=None, account_id="user-one", verified=True):
    with catalog.connect() as conn:
        conn.execute(
            "INSERT INTO genchi_private.accounts(id,email,verified_at) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
            (account_id, account_id + "@example.test", NOW if verified else None),
        )
        conn.execute(
            "INSERT INTO genchi_private.follows(id,account_id,target_type,target_id) VALUES(%s,%s,%s,%s)",
            (
                str(uuid.uuid4()),
                account_id,
                "ACTIVITY" if activity_id else "SUBJECT",
                activity_id or "idolmaster",
            ),
        )
    return account_id


def test_date_precision_rejects_invented_clock_and_naive_times():
    with pytest.raises(ValidationError):
        Moment(precision="TIME", starts_at="2030-01-01T00:00:00")
    with pytest.raises(ValidationError):
        Moment(precision="DATE", starts_on="2030-01-01", starts_at="2030-01-01T00:00:00+09:00")
    with pytest.raises(ValidationError):
        Moment(precision="TIME", starts_at=NOW, ends_at=NOW - timedelta(hours=1))
    assert legacy_time(datetime(2030, 1, 1, 15, tzinfo=UTC)).precision == "DATE"
    assert EvidenceInput(excerpt="source", url="javascript:alert(1)").url is None


def test_cross_publisher_activity_and_round_merge_preserves_evidence(catalog):
    first = activity("official:event")
    first.url = "https://official.test/live/2030"
    first.milestones[0].url = "https://tickets.test/2030"
    original = catalog.publish(first, historical=True)
    second = first.model_copy(deep=True)
    second.source_key = "publisher:article"
    second.title = "学園アイドルマスター TEST LIVE チケット情報"
    second.url += "?utm_source=publisher"
    second.evidence.source_id = "publisher"
    second.milestones[0].source_key = "article:round"
    second.milestones[0].round_key = "先行抽选"
    second.milestones[0].evidence.source_id = "publisher"
    assert catalog.publish(second, historical=True) == original
    assert catalog.publish(second, historical=True) == original
    with catalog.connect() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM catalog_activities").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) n FROM catalog_occurrences").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) n FROM catalog_milestones").fetchone()["n"] == 2
        assert conn.execute("SELECT COUNT(DISTINCT source_id) n FROM catalog_evidence").fetchone()["n"] == 2
        assert conn.execute("SELECT COUNT(*) n FROM catalog_external_ids WHERE key LIKE '%article:round'").fetchone()["n"] == 1


def test_reused_official_url_does_not_merge_another_edition_or_ticket_product(catalog):
    first = activity("official:one")
    first.url = "https://official.test/live/tour"
    first.milestones[0].url = "https://tickets.test/round"
    first.milestones[0].details = {"price_jpy": 10000}
    original = catalog.publish(first, historical=True)
    second = first.model_copy(deep=True)
    second.source_key = "publisher:two"
    second.title = "学園アイドルマスター ANOTHER LIVE"
    second.time.starts_at += timedelta(days=120)
    assert catalog.publish(second, historical=True) != original
    third = first.model_copy(deep=True)
    third.source_key = "publisher:three"
    third.milestones[0].source_key = "vip-round"
    third.milestones[0].round_key = "VIP"
    third.milestones[0].details = {"price_jpy": 20000}
    assert catalog.publish(third, historical=True) == original
    with catalog.connect() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM catalog_milestones WHERE activity_id=%s AND kind='TICKET'", (original,)).fetchone()["n"] == 2


def test_replay_timezone_correction_and_year_identity(catalog):
    item = activity()
    first = catalog.publish(item)
    equivalent = item.model_copy(deep=True)
    equivalent.time.starts_at = item.time.starts_at.astimezone(
        __import__("zoneinfo").ZoneInfo("Asia/Tokyo")
    )
    assert catalog.publish(equivalent) == first
    with catalog.connect() as conn:
        assert conn.execute("SELECT count(*) n FROM catalog_changes").fetchone()["n"] == 3
    correction = activity(start=NOW + timedelta(days=22))
    correction.evidence.version_hash = "two"
    assert catalog.publish(correction) == first
    assert catalog.publish(activity("next-edition", NOW + timedelta(days=365))) != first
    with catalog.connect() as conn:
        node = conn.execute(
            "SELECT * FROM catalog_milestones WHERE activity_id=%s AND kind='START'", (first,)
        ).fetchone()
        assert node["revision"] == 2
        assert node["starts_at"] == correction.time.starts_at


def test_period_pass_and_first_dated_admission_have_separate_occurrences(catalog):
    period = activity("lawson:period").model_copy(update={
        "kind": "EXHIBITION", "time": Moment(precision="DATE", starts_on="2030-06-12", ends_on="2030-06-30"),
        "milestones": [],
    })
    dated = period.model_copy(update={
        "source_key": "lawson:dated", "occurrence_key": "lawson:dated",
        "time": Moment(precision="DATE", starts_on="2030-06-12"),
    })
    catalog.publish(period)
    catalog.publish(dated)
    changed = period.model_copy(update={"time": Moment(precision="DATE", starts_on="2030-06-12", ends_on="2030-07-01")})
    catalog.publish(changed)
    catalog.publish(dated)
    with catalog.connect() as conn:
        rows = conn.execute("SELECT starts_on,ends_on FROM catalog_occurrences ORDER BY ends_on NULLS LAST").fetchall()
    assert len(rows) == 2
    assert str(rows[0]["ends_on"]) == "2030-07-01"
    assert rows[1]["ends_on"] is None


def test_shared_round_multiple_occurrences_and_cross_activity_isolation(catalog):
    first = catalog.publish(activity())
    assert catalog.publish(activity("upstream:2", NOW + timedelta(days=21))) == first
    other = activity("other", NOW + timedelta(days=400))
    other_id = catalog.publish(other)
    with catalog.connect() as conn:
        nodes = conn.execute("SELECT * FROM catalog_milestones WHERE kind='TICKET'").fetchall()
        assert len(nodes) == 2
        node = next(n for n in nodes if n["activity_id"] == first)
        assert (
            conn.execute(
                "SELECT count(*) n FROM catalog_milestone_scopes WHERE milestone_id=%s",
                (node["id"],),
            ).fetchone()["n"]
            == 2
        )
        assert any(n["activity_id"] == other_id for n in nodes)


def test_conflict_rejection_preserves_published_activity(catalog):
    item = activity()
    aid = catalog.publish(item)
    item.milestones[0].time.ends_at += timedelta(days=1)
    item.milestones[0].evidence = proof(False, "conflict")
    catalog.publish(item)
    with catalog.connect() as conn:
        review = conn.execute("SELECT * FROM catalog_reviews").fetchone()
    assert catalog.approve_review(review["id"], "editor", False)
    with catalog.connect() as conn:
        assert (
            conn.execute(
                "SELECT publication FROM catalog_activities WHERE id=%s", (aid,)
            ).fetchone()["publication"]
            == "PUBLISHED"
        )
        assert conn.execute(
            "SELECT ends_at FROM catalog_milestones WHERE kind='TICKET'"
        ).fetchone()["ends_at"] == NOW + timedelta(days=3)


def test_review_promotion_adds_verified_evidence(catalog):
    value = activity(verified=False)
    value.publication = "REVIEW"
    with catalog.connect() as conn:
        Catalog.review(
            conn,
            key="candidate",
            reason="Needs verification",
            payload={"activity": value.model_dump(mode="json")},
        )
        review_id = conn.execute("SELECT id FROM catalog_reviews").fetchone()["id"]
    assert catalog.approve_review(review_id, "editor", True)
    assert not catalog.approve_review(review_id, "editor", True)
    with catalog.connect() as conn:
        assert (
            conn.execute("SELECT publication FROM catalog_activities").fetchone()["publication"]
            == "PUBLISHED"
        )
        assert (
            conn.execute("SELECT count(*) n FROM catalog_evidence WHERE verified").fetchone()["n"]
            >= 3
        )


def test_notification_dedup_and_send_once(catalog):
    aid = catalog.publish(activity(), historical=True)
    subscribe(catalog)
    subscribe(catalog, aid)
    plan(catalog, NOW)
    plan(catalog, NOW)
    with catalog.connect() as conn:
        jobs = conn.execute("SELECT * FROM genchi_private.mail_queue").fetchall()
        assert len(jobs) == 3  # opening, deadline, concert
        assert len(eligible_follows(conn, aid)) == 1
    sent = []
    assert deliver_one(
        catalog, now=NOW + timedelta(hours=1), transport=lambda *args: sent.append(args)
    )
    assert not deliver_one(
        catalog, now=NOW + timedelta(hours=1), transport=lambda *args: sent.append(args)
    )
    assert len(sent) == 1
    assert "JST" in sent[0][2] and "Asia/Shanghai" in sent[0][2]


def test_unsubscribe_and_revision_cancel_pending_deliveries(catalog):
    item = activity()
    aid = catalog.publish(item, historical=True)
    subscribe(catalog, aid)
    plan(catalog, NOW)
    item.milestones[0].time.starts_at += timedelta(hours=3)
    catalog.publish(item)
    with catalog.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) n FROM genchi_private.mail_queue WHERE kind='OPENING' AND status='CANCELED'"
            ).fetchone()["n"]
            == 1
        )
        conn.execute("UPDATE genchi_private.accounts SET unsubscribed=TRUE")
    sent = []
    while deliver_one(
        catalog, now=NOW + timedelta(days=25), transport=lambda *args: sent.append(args)
    ):
        pass
    assert not sent


def test_unverified_or_date_only_no_precise_mail(catalog):
    item = activity(verified=False)
    aid = catalog.publish(item, historical=True)
    subscribe(catalog, aid)
    plan(catalog, NOW)
    with catalog.connect() as conn:
        assert conn.execute("SELECT count(*) n FROM genchi_private.mail_queue").fetchone()["n"] == 0


def test_payment_requires_explicit_same_round_win(catalog):
    item = activity()
    item.milestones.append(
        MilestoneInput(
            source_key="payment:1",
            kind="PAYMENT",
            title="入金截止",
            requires="WON",
            round_key="round:1",
            evidence=proof(),
            time=Moment(precision="TIME", starts_at=NOW + timedelta(days=4)),
        )
    )
    aid = catalog.publish(item, historical=True)
    user_id = subscribe(catalog, aid)
    plan(catalog, NOW)
    with catalog.connect() as conn:
        job = conn.execute(
            "SELECT q.* FROM genchi_private.mail_queue q JOIN catalog_milestones m ON m.id=q.milestone_id WHERE m.kind='PAYMENT'"
        ).fetchone()
        user = conn.execute("SELECT * FROM genchi_private.accounts").fetchone()
        assert render_mail(conn, job, user, NOW) is None
        conn.execute(
            "INSERT INTO genchi_private.participation VALUES(%s,%s,'round:other','WON')",
            (user_id, aid),
        )
        assert render_mail(conn, job, user, NOW) is None
        conn.execute(
            "INSERT INTO genchi_private.participation VALUES(%s,%s,'round:1','WON')", (user_id, aid)
        )
        assert render_mail(conn, job, user, NOW)


def test_auth_tokens_once_csrf_and_private_isolation(catalog, monkeypatch):
    sent = []
    monkeypatch.setattr("genchi_product.auth.smtp_send", lambda *args, **kwargs: sent.append(args))
    client = TestClient(create_app(catalog), headers={"Origin": "http://localhost:13000"})
    assert client.get("/me").status_code == 401
    assert client.get("/admin/reviews").status_code == 401
    assert (
        client.post(
            "/auth/login",
            json={"email": "one@example.test"},
            headers={"Origin": "https://evil.test"},
        ).status_code
        == 403
    )
    assert client.post("/auth/login", json={"email": "one@example.test"}).status_code == 200
    code = re.search(r"\b[0-9]{6}\b", sent[0][2]).group(0)
    body = {"email": "one@example.test", "code": code}
    assert client.post("/auth/verify", json=body).status_code == 200
    assert client.post("/auth/verify", json=body).status_code == 400
    assert client.get("/me").json()["email"] == "one@example.test"
    assert client.get("/admin/reviews").status_code == 403
    own = client.put("/me/follows", json={"target_type": "SUBJECT", "target_id": "gakumas"}).json()[
        "id"
    ]
    with catalog.connect() as conn:
        subscribe(catalog, account_id="other-user")
        other = conn.execute(
            "SELECT id FROM genchi_private.follows WHERE account_id='other-user'"
        ).fetchone()["id"]
    client.delete("/me/follows/" + other)
    assert [f["id"] for f in client.get("/me").json()["follows"]] == [own]
    with catalog.connect() as conn:
        assert conn.execute(
            "SELECT id FROM genchi_private.follows WHERE id=%s", (other,)
        ).fetchone()
        account_id = conn.execute(
            "SELECT account_id FROM genchi_private.sessions WHERE token_hash=%s",
            (digest(client.cookies["genchi_session"]),),
        ).fetchone()["account_id"]
    assert (
        client.post(
            "/auth/unsubscribe", params={"token": unsubscribe_token(account_id)}
        ).status_code
        == 200
    )
    assert client.get("/me").json()["unsubscribed"]


def test_calendar_keeps_in_month_deadline_for_earlier_window(catalog):
    item = activity()
    item.milestones[0].time = Moment(
        precision="TIME", starts_at=NOW - timedelta(days=10), ends_at=NOW + timedelta(days=2)
    )
    catalog.publish(item)
    response = TestClient(create_app(catalog), headers={"Origin": "http://localhost:13000"}).get(
        "/calendar", params={"from_date": "2030-06-01", "to_date": "2030-07-01"}
    )
    assert response.status_code == 200
    assert any(n["kind"] == "TICKET" for n in response.json()["items"])


def test_failed_model_extraction_still_indexes_raw(catalog, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with catalog.connect() as conn:
        conn.execute(
            "INSERT INTO allfeeds.resources(source_id,external_id,content_hash,kind,title,content,url) VALUES('official','one','hash','official_news','原文标题','未抽取的原文','https://example.test/news')"
        )
    assert process_one(catalog)
    with catalog.connect() as conn:
        assert (
            conn.execute('SELECT "searchText" FROM "SearchDocument"').fetchone()["searchText"]
            == "原文标题 未抽取的原文"
        )
        assert conn.execute("SELECT status FROM catalog_jobs").fetchone()["status"] == "REVIEW"
        assert conn.execute("SELECT count(*) n FROM catalog_activities").fetchone()["n"] == 0


def test_llm_fabricated_evidence_never_enters_catalog(monkeypatch):
    for key, value in {
        "LLM_API_KEY": "test",
        "LLM_BASE_URL": "https://example.test",
        "LLM_MODEL": "test",
    }.items():
        monkeypatch.setenv(key, value)

    class Response:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "activities": [
                                        {"title": "Invented", "evidence": "not in the source"}
                                    ]
                                }
                            )
                        },
                    }
                ]
            }

    monkeypatch.setattr("genchi_product.pipeline.requests.post", lambda *a, **k: Response())
    with pytest.raises(ValueError, match="证据"):
        extract_text(
            {
                "id": 1,
                "source_id": "official",
                "external_id": "1",
                "content_hash": "h",
                "content": "Actual announcement",
            },
            [],
        )


def test_canceled_activity_suppresses_reminders_but_keeps_update(catalog):
    item = activity()
    aid = catalog.publish(item, historical=True)
    subscribe(catalog, aid)
    plan(catalog, NOW)
    item.status = "CANCELED"
    catalog.publish(item)
    plan(catalog, NOW)
    sent = []
    while deliver_one(
        catalog, now=NOW + timedelta(days=1), transport=lambda *args: sent.append(args)
    ):
        pass
    assert len(sent) == 1
    assert "活动信息更新" in sent[0][1]


def test_smtp_uncertainty_is_not_automatically_retried(catalog):
    aid = catalog.publish(activity(), historical=True)
    subscribe(catalog, aid)
    plan(catalog, NOW)

    def broken(*_):
        raise TimeoutError("SMTP may have accepted the message")

    assert deliver_one(catalog, now=NOW + timedelta(hours=1), transport=broken)
    assert not deliver_one(catalog, now=NOW + timedelta(hours=1), transport=broken)
    with catalog.connect() as conn:
        assert conn.execute(
            "SELECT status,attempts FROM genchi_private.mail_queue WHERE kind='OPENING'"
        ).fetchone() == {"status": "UNCERTAIN", "attempts": 1}


def test_changed_lead_time_reschedules_before_send(catalog):
    aid = catalog.publish(activity(), historical=True)
    subscribe(catalog, aid)
    plan(catalog, NOW)
    with catalog.connect() as conn:
        conn.execute(
            "UPDATE genchi_private.mail_queue SET status='CANCELED' WHERE kind<>'DEADLINE'"
        )
        conn.execute("UPDATE genchi_private.follows SET reminder_hours=2")
    sent = []
    assert deliver_one(
        catalog, now=NOW + timedelta(days=2), transport=lambda *args: sent.append(args)
    )
    assert not sent
    with catalog.connect() as conn:
        assert conn.execute(
            "SELECT due_at FROM genchi_private.mail_queue WHERE kind='DEADLINE'"
        ).fetchone()["due_at"] == NOW + timedelta(days=3, hours=-2)


def test_pending_llm_revision_does_not_change_public_activity(catalog, monkeypatch):
    original = activity()
    aid = catalog.publish(original, historical=True)
    pending = original.model_copy(deep=True)
    pending.publication = "REVIEW"
    pending.milestones[0].time.ends_at += timedelta(days=2)
    pending.evidence = proof(False)
    with catalog.connect() as conn:
        conn.execute(
            "INSERT INTO allfeeds.resources(source_id,external_id,content_hash,kind,title,content) VALUES('official','one','hash','official_news','活动修改','调整时间')"
        )
    monkeypatch.setattr("genchi_product.pipeline.extract_text", lambda *_: [pending])
    assert process_one(catalog)
    with catalog.connect() as conn:
        assert (
            conn.execute(
                "SELECT ends_at FROM catalog_milestones WHERE activity_id=%s AND kind='TICKET'",
                (aid,),
            ).fetchone()["ends_at"]
            == original.milestones[0].time.ends_at
        )
        assert conn.execute("SELECT count(*) n FROM catalog_reviews WHERE status='PENDING'").fetchone()["n"] == 1


@pytest.mark.parametrize("kind,title,expected", [
    ("ticket_booth", "公演受付一覧", "DONE"),
    ("ticket_act", "3公演通し券", "DONE"),
    ("ticket_reception", "3公演通し券 先行受付", "REVIEW"),
])
def test_asobi_containers_are_indexed_and_unmatched_receptions_still_require_review(catalog, monkeypatch, kind, title, expected):
    with catalog.connect() as conn:
        conn.execute(
            """INSERT INTO allfeeds.resources(source_id,external_id,content_hash,kind,title,content,attributes)
            VALUES('asobi','one','hash',%s,%s,'','{"source_type":"asobi_ticket","asobi_ticket":{}}')""",
            (kind, title),
        )
    monkeypatch.setattr("genchi_product.pipeline.extract_text", lambda *_: pytest.fail("native containers must not use the model"))
    assert process_one(catalog)
    with catalog.connect() as conn:
        assert conn.execute("SELECT status FROM catalog_jobs").fetchone()["status"] == expected
        assert conn.execute("SELECT count(*) n FROM catalog_activities").fetchone()["n"] == 0
        assert conn.execute('SELECT count(*) n FROM "SearchDocument" WHERE "entityType"=\'CONTENT\'').fetchone()["n"] == 1
        assert (
            conn.execute(
                "SELECT count(*) n FROM catalog_reviews WHERE status='PENDING'"
            ).fetchone()["n"]
            == (1 if expected == "REVIEW" else 0)
        )


def test_successful_reprocessing_closes_only_its_failure_and_keeps_candidate_pending(catalog, monkeypatch):
    with catalog.connect() as conn:
        conn.execute("INSERT INTO allfeeds.resources(source_id,external_id,content_hash,kind,title,content) VALUES('official','one','hash','official_news','ライブ','原文')")

    def invalid(*_):
        raise ValueError("invalid evidence")

    monkeypatch.setattr("genchi_product.pipeline.extract_text", invalid)
    assert process_one(catalog)
    with catalog.connect() as conn:
        conn.execute("UPDATE catalog_jobs SET status='PENDING',not_before=NOW()")
    candidate = activity(verified=False)
    candidate.publication = "REVIEW"
    monkeypatch.setattr("genchi_product.pipeline.extract_text", lambda *_: [candidate])
    assert process_one(catalog)
    with catalog.connect() as conn:
        rows = conn.execute("SELECT status,reviewed_by,payload ? 'activity' AS candidate FROM catalog_reviews ORDER BY created_at").fetchall()
        assert rows == [
            {"status": "REJECTED", "reviewed_by": "system:normalizer", "candidate": False},
            {"status": "PENDING", "reviewed_by": None, "candidate": True},
        ]
        assert conn.execute("SELECT count(*) n FROM catalog_activities").fetchone()["n"] == 0


def test_official_grouping_merges_legacy_sessions_and_keeps_links(catalog):
    one = activity("native:act:1")
    one.title = "DAY 1"
    two = activity("native:act:2", NOW + timedelta(days=21))
    two.title = "DAY 2"
    old_one, old_two = catalog.publish(one), catalog.publish(two)
    subscribe(catalog, old_two)
    one.title = two.title = "学園アイドルマスター TWO DAY LIVE"
    one.activity_key = two.activity_key = "native:booth:one"
    assert catalog.publish(one) == old_one
    assert catalog.publish(two) == old_one
    with catalog.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) n FROM catalog_activities WHERE publication='PUBLISHED'"
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) n FROM catalog_occurrences WHERE activity_id=%s", (old_one,)
            ).fetchone()["n"]
            == 2
        )
        assert eligible_follows(conn, old_one)
        assert (
            conn.execute("SELECT target_id FROM genchi_private.follows").fetchone()["target_id"]
            == old_one
        )
    response = TestClient(create_app(catalog), headers={"Origin": "http://localhost:13000"}).get("/activities/" + old_two)
    assert response.status_code == 200
    assert response.json()["id"] == old_one
    assert len(response.json()["milestones"]) >= 3


def test_multiple_node_updates_are_bundled_per_activity(catalog):
    item = activity()
    aid = catalog.publish(item, historical=True)
    subscribe(catalog, aid)
    item.time.starts_at += timedelta(days=1)
    item.milestones[0].time.ends_at += timedelta(days=1)
    catalog.publish(item)
    plan(catalog, NOW)
    plan(catalog, NOW)
    with catalog.connect() as conn:
        jobs = conn.execute(
            "SELECT * FROM genchi_private.mail_queue WHERE kind='UPDATE'"
        ).fetchall()
        assert len(jobs) == 1
        assert len(jobs[0]["payload"]["changes"]) == 2
        assert jobs[0]["due_at"] == NOW + timedelta(minutes=5)


def test_discovery_orders_future_opening_before_later_deadline(catalog):
    first = activity("first")
    first.title = "First ticket opens earlier"
    first.milestones[0].time = Moment(
        precision="TIME", starts_at=NOW + timedelta(days=1), ends_at=NOW + timedelta(days=10)
    )
    second = activity("second")
    second.title = "Second ticket opens later"
    second.milestones[0].time = Moment(
        precision="TIME", starts_at=NOW + timedelta(days=2), ends_at=NOW + timedelta(days=5)
    )
    first_id = catalog.publish(first)
    second_id = catalog.publish(second)
    result = TestClient(create_app(catalog), headers={"Origin": "http://localhost:13000"}).get("/activities").json()["items"]
    assert [a["id"] for a in result] == [first_id, second_id]


def agenda_client(catalog, account_id="agenda-user"):
    subscribe(catalog, account_id=account_id)
    token = "local-session-" + account_id
    with catalog.connect() as conn:
        conn.execute(
            "INSERT INTO genchi_private.sessions VALUES(%s,%s,NOW()+INTERVAL '1 hour')",
            (digest(token), account_id),
        )
    client = TestClient(create_app(catalog), headers={"Origin": "http://localhost:13000"})
    client.cookies.set("genchi_session", token)
    return client


def test_agenda_projects_real_boundaries_and_preserves_unknown_precision():
    from datetime import date

    from genchi_product.presentation import agenda_groups

    base = {
        "id": "ticket",
        "activity_id": "a",
        "activity_title": "Live",
        "activity_kind": "LIVE",
        "activity_status": "SCHEDULED",
        "status": "CONFIRMED",
        "kind": "TICKET",
        "title": "Round",
        "round_key": "one",
        "precision": "TIME",
        "starts_at": NOW - timedelta(days=20),
        "ends_at": NOW + timedelta(days=2),
        "starts_on": None,
        "ends_on": None,
    }
    dated = {
        **base,
        "id": "date",
        "precision": "DATE",
        "starts_at": None,
        "ends_at": None,
        "starts_on": date(2030, 6, 4),
        "ends_on": date(2030, 6, 4),
    }
    unknown = {**dated, "id": "unknown", "precision": "TBD", "starts_on": None, "ends_on": None}
    long_window = {**base, "id": "long", "ends_at": NOW + timedelta(days=40)}
    groups, ongoing, undated = agenda_groups(
        [base, dated, unknown, long_window], [], date(2030, 6, 1), date(2030, 6, 8)
    )
    assert [g["date"] for g in groups] == ["2030-06-03", "2030-06-04"]
    assert groups[0]["actions"][0]["boundary"] == "end"
    assert groups[1]["actions"][0]["at"] is None
    assert len(groups[1]["actions"]) == 1
    assert len(ongoing) == undated == 1
    # Two actual moments on the same day stay in one activity group.
    precise = {
        **base,
        "starts_at": NOW + timedelta(days=3, hours=1),
        "ends_at": NOW + timedelta(days=3, hours=2),
    }
    groups, _, _ = agenda_groups([precise], [], date(2030, 6, 1), date(2030, 6, 8))
    assert len(groups) == 1 and len(groups[0]["actions"]) == 2


def test_agenda_progress_is_round_scoped_resettable_and_private(catalog):
    value = activity()
    value.milestones.extend(
        [
            MilestoneInput(
                source_key="result",
                kind="RESULT",
                title="Results",
                round_key="round:1",
                requires="APPLIED",
                evidence=proof(),
                time=Moment(precision="TIME", starts_at=NOW + timedelta(days=4)),
            ),
            MilestoneInput(
                source_key="payment",
                kind="PAYMENT",
                title="Pay",
                round_key="round:1",
                requires="WON",
                evidence=proof(),
                time=Moment(precision="TIME", starts_at=NOW + timedelta(days=5)),
            ),
            MilestoneInput(
                source_key="other-ticket",
                kind="TICKET",
                title="Other round",
                round_key="round:2",
                evidence=proof(),
                time=Moment(precision="TIME", starts_at=NOW + timedelta(days=2)),
            ),
        ]
    )
    activity_id = catalog.publish(value)
    client = agenda_client(catalog)
    params = {"from_date": "2030-06-01", "to_date": "2030-06-08"}

    def actions():
        response = client.get("/me/agenda", params=params)
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "no-store"
        return [a for g in response.json()["items"] for a in g["actions"]]

    assert {a["node"]["kind"] for a in actions()} == {"TICKET", "RESULT", "PAYMENT"}
    for state, excluded in [
        ("APPLIED", {"TICKET"}),
        ("WON", {"TICKET", "RESULT"}),
        ("PURCHASED", {"TICKET", "RESULT", "PAYMENT"}),
    ]:
        assert (
            client.put(
                "/me/participation/" + activity_id, json={"round_key": "round:1", "status": state}
            ).status_code
            == 200
        )
        assert not [
            a
            for a in actions()
            if a["node"]["round_key"] == "round:1" and a["node"]["kind"] in excluded
        ]
        assert [a for a in actions() if a["node"]["round_key"] == "round:2"]
    assert (
        client.delete(
            "/me/participation/" + activity_id, params={"round_key": "round:1"}
        ).status_code
        == 200
    )
    assert {a["node"]["kind"] for a in actions()} == {"TICKET", "RESULT", "PAYMENT"}
    assert all(a["participation"] is None for a in actions())
    assert TestClient(create_app(catalog), headers={"Origin": "http://localhost:13000"}).get("/me/agenda", params=params).status_code == 401
    assert client.get("/me/agenda", params={**params, "to_date": "2030-08-01"}).status_code == 422
    with catalog.connect() as conn:
        conn.execute("UPDATE genchi_private.accounts SET unsubscribed=TRUE")
    assert actions()  # Email opt-out does not hide agenda.


def test_discovery_filters_event_dates_and_city_on_same_occurrence(catalog):
    first = activity("date-a")
    first.city = "Tokyo"
    first.time = Moment(precision="TIME", starts_at=datetime(2030, 6, 1, 15, tzinfo=UTC))
    first_id = catalog.publish(first)
    second = activity("date-b")
    second.city = "Osaka"
    second.time = Moment(precision="DATE", starts_on="2030-06-10", ends_on="2030-06-12")
    assert catalog.publish(second) == first_id
    client = TestClient(create_app(catalog), headers={"Origin": "http://localhost:13000"})

    def ids(**params):
        response = client.get("/activities", params=params)
        assert response.status_code == 200, response.text
        return [a["id"] for a in response.json()["items"]]

    assert (
        ids(from_date="2030-06-01", to_date="2030-06-01") == []
    )  # UTC date is not the event date in Japan.
    assert ids(from_date="2030-06-02", to_date="2030-06-02", city="Tokyo") == [first_id]
    assert ids(from_date="2030-06-02", to_date="2030-06-02", city="Osaka") == []
    assert ids(from_date="2030-06-11", to_date="2030-06-11", city="Osaka") == [first_id]
    assert (
        client.get(
            "/activities", params={"from_date": "2030-06-15", "to_date": "2030-06-01"}
        ).status_code
        == 422
    )


def test_followed_activities_and_agenda_paginate_without_100_item_cutoff(catalog):
    for n in range(105):
        value = activity("many:" + str(n))
        value.title = f"Distinct event {n:03d}"
        catalog.publish(value)
    client = agenda_client(catalog)
    first = client.get("/me/activities", params={"limit": 100}).json()
    last = client.get("/me/activities", params={"limit": 100, "page": 2}).json()
    assert first["total"] == last["total"] == 105
    assert len(first["items"]) == 100 and len(last["items"]) == 5
    assert len({a["id"] for a in first["items"] + last["items"]}) == 105
    params = {"from_date": "2030-06-01", "to_date": "2030-06-02", "limit": 100}
    first = client.get("/me/agenda", params=params).json()
    last = client.get("/me/agenda", params={**params, "page": 2}).json()
    assert first["total"] == 105 and first["action_count"] == 105
    assert len(first["items"]) == 100 and len(last["items"]) == 5
    exported = client.get("/calendar", params={**params, "limit": 20}).json()
    assert exported["total"] == 105 and len(exported["items"]) == 20


def test_applied_ticket_reminders_stop_only_for_that_round(catalog):
    aid = catalog.publish(activity(), historical=True)
    user_id = subscribe(catalog, aid)
    plan(catalog, NOW)
    with catalog.connect() as conn:
        job = conn.execute(
            "SELECT q.* FROM genchi_private.mail_queue q JOIN catalog_milestones m ON m.id=q.milestone_id WHERE m.kind='TICKET' LIMIT 1"
        ).fetchone()
        user = conn.execute("SELECT * FROM genchi_private.accounts").fetchone()
        assert render_mail(conn, job, user, NOW) is not None
        conn.execute(
            "INSERT INTO genchi_private.participation VALUES(%s,%s,'round:other','APPLIED')",
            (user_id, aid),
        )
        assert render_mail(conn, job, user, NOW) is not None
        conn.execute(
            "INSERT INTO genchi_private.participation VALUES(%s,%s,'round:1','APPLIED')",
            (user_id, aid),
        )
        assert render_mail(conn, job, user, NOW) is None


def test_names_backfill_preserves_identity_facts_notifications_and_replay(catalog):
    from genchi_product.naming import normalize_catalog

    item = activity()
    item.title = "学園アイドルマスター LIVE TOUR -標-"
    item.milestones[0].title = "先着 ★一般発売"
    aid = catalog.publish(item)
    subscribe(catalog, aid)
    plan(catalog, now=NOW)
    with catalog.connect() as conn:
        before = {
            "facts": conn.execute(
                "SELECT id,title,revision,updated_at FROM catalog_activities"
            ).fetchall(),
            "nodes": conn.execute(
                "SELECT id,title,round_key,revision,starts_at,ends_at FROM catalog_milestones ORDER BY id"
            ).fetchall(),
            "ids": conn.execute("SELECT * FROM catalog_external_ids ORDER BY key").fetchall(),
            "mail": conn.execute("SELECT * FROM genchi_private.mail_queue ORDER BY id").fetchall(),
            "changes": conn.execute("SELECT count(*) n FROM catalog_changes").fetchone()["n"],
        }
        conn.execute("UPDATE catalog_activities SET title_zh='Incorrect old English translation'")
        conn.execute("UPDATE catalog_milestones SET title_zh=NULL")
        conn.execute("UPDATE catalog_occurrences SET label_zh=NULL")
    preview = normalize_catalog(catalog)
    assert preview["changed"] >= 3
    with catalog.connect() as conn:
        assert (
            conn.execute("SELECT title_zh FROM catalog_activities").fetchone()["title_zh"]
            == "Incorrect old English translation"
        )
    assert normalize_catalog(catalog, apply=True)["changed"] == preview["changed"]
    with catalog.connect() as conn:
        assert (
            conn.execute("SELECT title_zh FROM catalog_activities").fetchone()["title_zh"]
            == "学园偶像大师 巡回演唱会 -標-"
        )
        assert (
            conn.execute("SELECT title_zh FROM catalog_milestones WHERE kind='TICKET'").fetchone()[
                "title_zh"
            ]
            == "一般贩售（先到先得）"
        )
        assert (
            conn.execute("SELECT id,title,revision,updated_at FROM catalog_activities").fetchall()
            == before["facts"]
        )
        assert (
            conn.execute(
                "SELECT id,title,round_key,revision,starts_at,ends_at FROM catalog_milestones ORDER BY id"
            ).fetchall()
            == before["nodes"]
        )
        assert (
            conn.execute("SELECT * FROM catalog_external_ids ORDER BY key").fetchall()
            == before["ids"]
        )
        assert (
            conn.execute("SELECT * FROM genchi_private.mail_queue ORDER BY id").fetchall()
            == before["mail"]
        )
        assert (
            conn.execute("SELECT count(*) n FROM catalog_changes").fetchone()["n"]
            == before["changes"]
        )
        history = conn.execute("SELECT count(*) n FROM catalog_name_history").fetchone()["n"]
        rendered = render_mail(
            conn,
            conn.execute("SELECT * FROM genchi_private.mail_queue WHERE kind='OPENING'").fetchone(),
            conn.execute("SELECT * FROM genchi_private.accounts WHERE id='user-one'").fetchone(),
            NOW + timedelta(hours=1),
        )
        assert "一般贩售（先到先得）" in rendered[0]
        assert "学园偶像大师 巡回演唱会 -標-" in rendered[1]
    assert normalize_catalog(catalog, apply=True)["changed"] == 0
    catalog.publish(item)
    with catalog.connect() as conn:
        assert (
            conn.execute("SELECT count(*) n FROM catalog_name_history").fetchone()["n"] == history
        )
    client = TestClient(create_app(catalog), headers={"Origin": "http://localhost:13000"})
    for q in ["学園アイドルマスター", "学园偶像大师"]:
        assert client.get("/activities", params={"q": q, "view": "all"}).json()["total"] == 1
    assert client.get("/activities/" + aid).json()["milestones"][0]["title_zh"]
    events = client.get(
        "/calendar", params={"from_date": "2030-06-01", "to_date": "2030-07-01"}
    ).json()["items"]
    assert all(n["activity_title"] == "学园偶像大师 巡回演唱会 -標-" for n in events)


def test_name_editor_requires_admin_rejects_stale_source_and_survives_replay(catalog, monkeypatch):
    from genchi_product.naming import normalize_catalog, sync_name

    item = activity()
    item.title = "知らない祭典"
    aid = catalog.publish(item)
    client = TestClient(create_app(catalog), headers={"Origin": "http://localhost:13000"})
    payload = dict(
        entity_type="ACTIVITY", entity_id=aid, source_text=item.title, display_text="祭典专名"
    )
    assert client.get("/admin/names").status_code == 401
    assert client.post("/admin/names", json=payload).status_code == 401
    monkeypatch.setenv("PRODUCT_ADMIN_TOKEN", "test-editor-token")
    client.headers["X-Admin-Token"] = "test-editor-token"
    review = client.get("/admin/names").json()
    assert review["total"] >= 1
    assert client.post("/admin/names", json={**payload, "source_text": "stale"}).status_code == 409
    assert client.post("/admin/names", json=payload).status_code == 200
    catalog.publish(item)
    normalize_catalog(catalog, apply=True)
    with catalog.connect() as conn:
        assert (
            conn.execute("SELECT title_zh FROM catalog_activities WHERE id=%s", (aid,)).fetchone()[
                "title_zh"
            ]
            == "祭典专名"
        )
        conn.execute("UPDATE catalog_activities SET title=%s WHERE id=%s", ("新しい祭典", aid))
        result = sync_name(conn, "ACTIVITY", aid)
        assert result["display"] != "祭典专名" and result["state"] == "REVIEW"
    assert client.post("/admin/names", json=payload).status_code == 409
    assert (
        client.post("/admin/names", json={**payload, "entity_type": "accounts"}).status_code == 422
    )


def test_glossary_matching_protects_names_and_ticket_qualifications():
    from genchi_product.naming import normalize_name

    assert (
        normalize_name("学園アイドルマスター LIVE TOUR -標-", "ACTIVITY").text
        == "学园偶像大师 巡回演唱会 -標-"
    )
    assert (
        normalize_name("animate × Love Live! 新メニュー", "ACTIVITY").text
        == "animate × Love Live! 新菜单"
    )
    assert normalize_name("Animated Film", "ACTIVITY").text == "Animated Film"
    assert normalize_name("未登録のまつり", "ACTIVITY").state == "REVIEW"
    assert (
        normalize_name("先着 ★一般発売", "MILESTONE").text
        == normalize_name("一般発売 先着", "MILESTONE").text
    )
    name = normalize_name("アソビストアプレミアム会員2次先行 · 入金截止", "MILESTONE").text
    assert name == "ASOBI STORE 付费会员第2轮先行 · 付款截止"
    name = normalize_name("抽選 ◆24日2枚車椅子/オフィシャル先行", "MILESTONE").text
    assert "24日" in name and "2张轮椅席" in name and "官方先行" in name and "抽选" in name
    name = normalize_name(
        "アニメーション 呪術廻戦展 「懐玉･玉折」「渋谷事変」 フリー入場券", "ACTIVITY"
    ).text
    assert "免费" not in name


def test_extraction_injects_shared_glossary_and_keeps_original_names(monkeypatch):
    import requests
    from genchi_normalizer.glossary import load_glossary

    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_BASE_URL", "https://model.example.test")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    document = "学園アイドルマスター 新しいライブ。アソビストア一般会員先行。"
    payload = {
        "activities": [
            {
                "title": "学園アイドルマスター 新しいライブ",
                "title_zh": "学园偶像大师全新演唱会",
                "evidence": document,
                "milestones": [
                    {
                        "title": "アソビストア一般会員先行",
                        "title_zh": "ASOBI STORE 普通会员先行",
                        "kind": "TICKET",
                        "round": "アソビストア一般会員先行",
                        "evidence": "アソビストア一般会員先行。",
                    }
                ],
            }
        ]
    }
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]
            }

    def post(_url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(requests, "post", post)
    items = extract_text(
        {
            "id": 1,
            "source_id": "official",
            "external_id": "1",
            "content_hash": "one",
            "content": document,
        },
        [],
    )
    system = captured["json"]["messages"][0]["content"]
    assert load_glossary()["version"] in system and "animate" in system
    assert "一般贩售" in system and "学园偶像大师" in system
    assert items[0].title == payload["activities"][0]["title"]
    assert items[0].title_zh == "学园偶像大师全新演唱会"
    assert items[0].milestones[0].round_key == "アソビストア一般会員先行"
    assert items[0].milestones[0].title == "アソビストア一般会員先行"
    assert items[0].publication == "REVIEW" and not items[0].evidence.verified


def test_lawson_round_repair_preserves_legacy_id_and_splits_scopes(catalog, monkeypatch):
    repair = runpy.run_path('deploy/repair-lawson-rounds.py')['repair']
    weekday, weekend = activity('weekday'), activity('weekend', start=NOW + timedelta(days=21))
    old_node = weekday.milestones[0].model_copy(update={
        'source_key': 'native-ticket:lawson:old', 'platform': 'lawson', 'round_key': 'lawson:old', 'notes': 'weekend',
    })
    weekday = weekday.model_copy(update={'milestones': [old_node]})
    weekend = weekend.model_copy(update={'milestones': [old_node]})
    catalog.publish(weekday)
    catalog.publish(weekend)
    historical = weekend.model_copy(update={
        'source_key': 'older-weekend', 'occurrence_key': 'older-weekend',
        'time': Moment(precision='TIME', starts_at=NOW + timedelta(days=19)),
    })
    catalog.publish(historical)
    weekday_node = old_node.model_copy(update={
        'source_key': 'native-ticket:lawson:weekday', 'round_key': 'lawson:weekday', 'notes': 'weekday',
        'time': Moment(precision='TIME', starts_at=old_node.time.starts_at, ends_at=NOW + timedelta(days=4)),
    })
    weekend_node = old_node.model_copy(update={'source_key': 'native-ticket:lawson:weekend', 'round_key': 'lawson:weekend'})
    items = [weekday.model_copy(update={'milestones': [weekday_node]}), weekend.model_copy(update={'milestones': [weekend_node]})]
    monkeypatch.setitem(repair.__globals__, 'structured', lambda *_: items)
    resources = [{'attributes': {'ticket_page': {'events': [{'ticketWindows': [
        {'id': 'weekday', 'legacyId': 'old'}, {'id': 'weekend', 'legacyId': 'old'},
    ]}]}}}]
    with catalog.connect() as conn:
        old_id = conn.execute("SELECT id FROM catalog_milestones WHERE round_key='lawson:old'").fetchone()['id']
        plan = repair(conn, resources, [])
        assert len(plan) == 1 and len(plan[0]['removed_scopes']) == 1
        assert conn.execute("SELECT count(*) n FROM catalog_milestone_scopes WHERE milestone_id=%s", (old_id,)).fetchone()['n'] == 3
        old_revision = conn.execute("SELECT revision FROM catalog_milestones WHERE id=%s", (old_id,)).fetchone()['revision']
        assert repair(conn, resources, [], apply=True) == plan
    for item in items:
        catalog.publish(item)
    with catalog.connect() as conn:
        rounds = conn.execute("SELECT id,round_key FROM catalog_milestones WHERE kind='TICKET' ORDER BY round_key").fetchall()
        assert len(rounds) == 2
        assert next(row['id'] for row in rounds if row['round_key'] == 'lawson:weekend') == old_id
        assert conn.execute("SELECT revision FROM catalog_milestones WHERE id=%s", (old_id,)).fetchone()['revision'] == old_revision
        assert conn.execute("SELECT count(*) n FROM catalog_milestone_scopes WHERE milestone_id=%s", (old_id,)).fetchone()['n'] == 2
        assert repair(conn, resources, [], apply=True) == []
