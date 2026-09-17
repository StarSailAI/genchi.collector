"""Catalogue grounding, date precision and the shared, durable Q&A limit."""

import hashlib
import hmac
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock

import psycopg
import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient
from genchi_product.api import create_app
from genchi_product.assistant import (
    SearchPlan,
    answer_question,
    complete,
    grounded_context,
    retrieve,
)
from genchi_product.home import countdown, featured, select_cards, select_music_cards
from test_product import activity
from test_product import catalog as product_catalog

catalog = product_catalog
NOW = datetime(2030, 6, 1, 3, tzinfo=UTC)


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setenv("ASK_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("ASK_LLM_API_KEY", "test-only-never-network")
    call = Mock(return_value={"status": "out_of_scope", "answer": "Outside event scope", "sources": []})
    monkeypatch.setattr("genchi_product.ask_agent.run_agent", call)
    return call


def test_countdown_never_invents_deadline_or_date_precision():
    row = dict(kind="TICKET", precision="TIME", starts_at=NOW + timedelta(days=1),
               ends_at=None, starts_on=None, ends_on=None)
    assert countdown(row, NOW) is None  # opening alone is not a deadline
    row["ends_at"] = NOW + timedelta(days=2)
    assert countdown(row, NOW)["days_remaining"] == 2
    assert countdown(row, row["ends_at"]) is None
    row.update(precision="DATE", starts_on=date(2030, 5, 28), ends_on=date(2030, 6, 1))
    card = countdown(row, NOW)
    assert card["target_at"] is None and card["days_remaining"] == 0
    assert countdown(row, NOW + timedelta(days=1)) is None
    row["precision"] = "TBD"
    assert countdown(row, NOW) is None


def test_selection_is_diverse_and_deduplicates_activity():
    rows = [dict(id=str(i), activity_id=str(i // 2), follow_count=0, source_count=2,
                 days_remaining=i + 1, subject_slug="a" if i < 6 else "b",
                 boundary="deadline" if i % 2 else "start") for i in range(20)]
    selected = select_cards(rows)
    assert len(selected) == len({r["activity_id"] for r in selected}) == 6
    assert {r["subject_slug"] for r in selected} == {"a", "b"}
    assert selected == select_cards(list(reversed(rows)))


def test_music_selection_balances_urgency_and_popularity():
    rows = [
        dict(id="far", activity_id="far", follow_count=100, source_count=5, days_remaining=37),
        dict(id="soon", activity_id="soon", follow_count=1, source_count=1, days_remaining=3),
        dict(id="popular", activity_id="popular", follow_count=8, source_count=4, days_remaining=12),
        dict(id="popular-duplicate", activity_id="popular", follow_count=8, source_count=4, days_remaining=12),
        dict(id="next", activity_id="next", follow_count=0, source_count=2, days_remaining=18),
    ]
    selected = select_music_cards(rows)
    assert "far" not in {row["id"] for row in selected}
    assert [row["activity_id"] for row in selected] == ["popular", "soon", "next"]


def test_global_limit_commits_across_concurrent_requests_and_process_clients(catalog, model):
    def ask(_):
        try:
            return answer_question(catalog, "When is MyGO live?")["status"]
        except HTTPException as exc:
            assert exc.status_code == 429
            assert 1 <= int(exc.headers["Retry-After"]) <= 3600
            return "limited"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(ask, range(8)))
    assert results.count("out_of_scope") == 1
    assert results.count("limited") == 7
    assert model.call_count == 1
    with catalog.connect() as conn:
        conn.execute("UPDATE genchi_private.auth_limits SET window_start=NOW()-INTERVAL '61 minutes'")
    assert ask(0) == "out_of_scope"


def test_model_failure_cannot_amplify_cost_via_retry(catalog, model):
    model.side_effect = requests.Timeout("sensitive provider details")
    with pytest.raises(HTTPException) as error:
        answer_question(catalog, "MyGO live?")
    assert error.value.status_code == 503 and "sensitive" not in str(error.value.detail)
    with pytest.raises(HTTPException) as error:
        answer_question(catalog, "MyGO live?")
    assert error.value.status_code == 429 and model.call_count == 1


def test_unconfigured_model_does_not_consume_slot(catalog, monkeypatch):
    monkeypatch.delenv("ASK_LLM_API_KEY", raising=False)
    with pytest.raises(HTTPException) as error:
        answer_question(catalog, "MyGO live?")
    assert error.value.status_code == 503
    with catalog.connect() as conn:
        assert conn.execute("SELECT count(*) n FROM genchi_private.auth_limits").fetchone()["n"] == 0


def test_proxy_signature_origin_validation_and_login_share_limit(catalog, model, monkeypatch):
    key = "test-proxy-signing-key-12345678901234567890"
    monkeypatch.setenv("PRODUCT_PROXY_SECRET", key)
    client = TestClient(create_app(catalog), headers={"Origin": "http://localhost:13000"})
    body = {"question": "MyGO next live?"}
    assert client.post("/ask", json=body).status_code == 403
    stamp = str(int(time.time()))
    signature = hmac.new(key.encode(), f"{stamp}\nPOST\n/ask\nshared-web".encode(), hashlib.sha256).hexdigest()
    headers = {"X-Genchi-Client-IP": "shared-web", "X-Genchi-Proxy-Time": stamp, "X-Genchi-Proxy-Signature": signature}
    assert client.post("/ask", json=body, headers={**headers, "Origin": "https://evil.test"}).status_code == 403
    assert client.post("/ask", json={"question": " " * 4}, headers=headers).status_code == 422
    assert client.post("/ask", json=body, headers=headers).status_code == 200
    # Auth state is intentionally not part of the quota key.
    other = TestClient(create_app(catalog), headers={"Origin": "http://localhost:13000"},
                       cookies={"genchi_session": "arbitrary-token-cannot-bypass-global-limit"})
    response = other.post("/ask", json=body, headers=headers)
    assert response.status_code == 429
    assert response.headers["cache-control"] == "no-store" and "retry-after" in response.headers
    assert model.call_count == 1


def test_retrieval_uses_only_published_records_and_literal_keywords(catalog):
    item = activity(start=datetime.now(UTC) + timedelta(days=10))
    published = catalog.publish(item)
    draft = item.model_copy(deep=True)
    draft.source_key = "draft:only"
    draft.title = "Private draft"
    draft.publication = "REVIEW"
    draft.occurrence_key = "draft:occurrence"
    draft.milestones[0].source_key = "draft:ticket"
    catalog.publish(draft)
    with catalog.connect() as conn:
        rows, _ = retrieve(conn, SearchPlan(relevant=True, keywords=[], subjects=["gakumas"]))
        assert [r["id"] for r in rows] == [published]
        assert rows[0]["evidence"] and rows[0]["milestones"]
        rows, _ = retrieve(conn, SearchPlan(relevant=True, keywords=["%"], subjects=[]))
        assert not rows


def test_answers_reject_fabricated_citations(catalog, model):
    model.side_effect = ValueError("Citation must be an exact excerpt of a source actually read")
    with pytest.raises(HTTPException) as error:
        answer_question(catalog, "Gakumas next live?")
    assert error.value.status_code == 503


def test_database_timeout_is_redacted(catalog, model):
    model.side_effect = psycopg.errors.QueryCanceled("sensitive query details")
    with pytest.raises(HTTPException) as error:
        answer_question(catalog, "Gakumas next live?")
    assert error.value.status_code == 503 and "sensitive" not in str(error.value.detail)


def test_insufficient_agent_answer_is_preserved(catalog, model):
    model.return_value = {"status": "insufficient", "answer": "资料不足，这不代表官方尚未公布。", "sources": []}
    answer = answer_question(catalog, "Gakumas next live?")
    assert answer["status"] == "insufficient" and model.call_count == 1
    assert "这不代表官方尚未公布" in answer["answer"]
    assert answer["sources"] == []


def test_featured_selection_persists_and_immediately_removes_cancelled(catalog):
    item = activity(start=datetime.now(UTC) + timedelta(days=10))
    item.milestones[0].time.starts_at = datetime.now(UTC) - timedelta(days=1)
    item.milestones[0].time.ends_at = datetime.now(UTC) + timedelta(days=2)
    key = catalog.publish(item)
    first = featured(catalog)
    assert len(first["items"]) == 1 and first["items"][0]["activity_id"] == key
    assert first["items"][0]["boundary"] == "deadline"
    assert "follow_count" not in first["items"][0]
    assert featured(catalog)["selected_at"] == first["selected_at"]
    with catalog.connect() as conn:
        conn.execute("UPDATE catalog_activities SET status='CANCELED' WHERE id=%s", (key,))
    assert featured(catalog)["items"] == []


def test_model_request_is_bounded_and_uses_flash_non_thinking(monkeypatch):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}']
    post = Mock(return_value=response)
    monkeypatch.setattr("genchi_product.assistant.requests.post", post)
    assert complete(("https://api.deepseek.com/v1/chat/completions", "secret", "deepseek-flash"),
                    "instruction", {}, 600) == {}
    payload = post.call_args.kwargs["json"]
    assert payload["model"] == "deepseek-flash" and payload["max_tokens"] == 600
    assert payload["thinking"] == {"type": "disabled"}
    assert "Genchi editorial glossary" in payload["messages"][0]["content"]
    assert post.call_args.kwargs["allow_redirects"] is False


def test_context_masks_unverified_times_and_internal_source_ids():
    rows = [{"id": "real-activity", "next_at": "2030-01-01T09:00:00Z", "summary": "Unverified date",
             "milestones": [{"id": "real-node", "kind": "START", "status": "CONFIRMED", "verified": False,
                             "occurrence_ids": ["real-occurrence"], "precision": "TIME", "starts_at": "2030-01-01T09:00:00Z"}],
             "occurrences": [{"id": "real-occurrence", "precision": "TIME", "starts_at": "2030-01-01T09:00:00Z"}],
             "evidence": [{"verified": False, "excerpt": "Unverified time at 09:00"}]}]
    context, sources = grounded_context(rows)
    assert sources["A1"]["id"] == "real-activity"
    assert context[0]["id"] == "A1" and "next_at" not in context[0]
    assert context[0]["summary"] == "" and context[0]["evidence"] == []
    for node in context[0]["occurrences"] + context[0]["milestones"]:
        assert node["precision"] == "TBD" and node["starts_at"] is None
    assert rows[0]["occurrences"][0]["starts_at"] is not None
