from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from genchi_product import batch_review
from genchi_product.domain import ActivityInput, EvidenceInput, MilestoneInput, Moment
from genchi_product.pipeline import extract_text, structured

QUOTE = "2026年9月25日 18:00 開演。学園アイドルマスター 東京公演。"


def candidate_row(key="review-1"):
    proof = EvidenceInput(source_id="official", external_id="news-1",
                          version_hash="hash-1", excerpt=QUOTE, method="llm:catalog-v3")
    activity = ActivityInput(
        source_key="news-1:activity", title="学園アイドルマスター 東京公演",
        kind="LIVE", subject_slugs=["gakumas"], venue="東京",
        time=Moment(precision="TIME", starts_at="2026-09-25T18:00:00+09:00"),
        evidence=proof,
        milestones=[MilestoneInput(source_key="start", kind="START", title="開演",
                                   time=Moment(precision="TIME", starts_at="2026-09-25T18:00:00+09:00"),
                                   evidence=proof)],
    )
    return {"id": key, "payload": {"activity": activity.model_dump(mode="json"), "matches": []},
            "content": QUOTE, "current_hash": "hash-1", "source_id": "official",
            "external_id": "news-1", "source_title": "公演発表",
            "source_url": "https://example.test/news/1",
            "attributes": {"source_role": "official_operator"}}


def test_hard_gate_requires_current_official_source_and_every_quote():
    row = candidate_row()
    assert batch_review.hard_gate(row)[0] is True
    row["attributes"]["source_role"] = "community"
    assert batch_review.hard_gate(row)[0] is False
    row["attributes"]["source_role"] = "official_operator"
    row["payload"]["activity"]["venue"] = None
    assert batch_review.hard_gate(row)[0] is False
    row["payload"]["activity"]["venue"] = "東京"
    row["payload"]["activity"]["milestones"][0]["evidence"]["excerpt"] = "missing"
    assert batch_review.hard_gate(row)[0] is False
    row = candidate_row()
    row["current_hash"] = "new-version"
    assert batch_review.hard_gate(row)[0] is False


def test_jpop_native_ticket_can_publish_after_independent_review():
    resource = {
        "source_id": "eplus-jpop-tickets", "external_id": "eplus:detail:123",
        "content_hash": "hash-1", "title": "架空歌手 LIVE",
        "url": "https://eplus.jp/sf/detail/123", "kind": "eplus_ticket_page",
        "attributes": {"source_type": "eplus_ticket", "eplus_ticket": {
            "platform": "eplus", "discoveryScope": "jpop", "events": [{
                "id": "123-P1", "name": "架空歌手 LIVE",
                "startsAt": "2026-11-20T19:00:00+09:00",
                "venue": {"name": "テストホール", "prefecture": "東京都"},
                "ticketWindows": [{"id": "round-1", "phaseLabelJa": "一般発売",
                                   "opensAt": "2026-09-20T10:00:00+09:00",
                                   "closesAt": "2026-11-19T23:59:00+09:00"}],
            }],
        }}, "tags": [],
    }
    candidate = structured(resource, [])[0]
    row = {"id": "jpop-one", "payload": {"activity": candidate.model_dump(mode="json"),
                                         "matches": []},
           "current_hash": resource["content_hash"],
           "source_id": resource["source_id"], "external_id": resource["external_id"],
           "source_title": resource["title"], "source_url": resource["url"],
           "source_kind": resource["kind"], "tags": [], "attributes": resource["attributes"]}
    assert candidate.subject_slugs == [] and candidate.publication == "REVIEW"
    assert batch_review.hard_gate(row, [])[0] is True
    row["payload"]["activity"]["occurrence_role"] = "ADMISSION"
    assert batch_review.hard_gate(row, [])[0] is False
    row["payload"]["activity"] = candidate.model_dump(mode="json")
    row["payload"]["activity"]["venue"] = "別のホール"
    assert batch_review.hard_gate(row, [])[0] is False
    row["payload"]["activity"] = candidate.model_dump(mode="json")
    resource["attributes"]["eplus_ticket"]["events"][0]["startsAt"] = "2026-11-21T19:00:00+09:00"
    assert batch_review.hard_gate(row, [])[0] is False


def test_approved_jpop_ticket_is_published_by_batch_worker(monkeypatch):
    resource = {
        "source_id": "pia-jpop-tickets", "external_id": "pia:event:123",
        "content_hash": "hash-1", "title": "架空歌手 LIVE",
        "url": "https://t.pia.jp/pia/event/event.do?eventBundleCd=123",
        "kind": "ticket_page", "tags": [],
        "attributes": {"source_type": "pia_ticket", "ticket_page": {
            "platform": "pia", "discoveryScope": "jpop",
            "performerName": "架空歌手", "formalEventTitle": "架空歌手 LIVE",
            "titleEvidence": "「架空歌手 LIVE」一般発売", "events": [{
                "id": "123-P1", "name": "架空歌手 LIVE",
                "startsAt": "2026-11-20T19:00:00+09:00",
                "venue": {"name": "テストホール", "prefecture": "東京都"},
                "ticketWindows": [{"id": "round-1", "phaseLabelJa": "一般発売",
                                   "opensAt": "2026-09-20T10:00:00+09:00"}],
            }],
        }},
    }
    candidate = structured(resource, [])[0]
    row = {"id": "jpop-pia", "payload": {"activity": candidate.model_dump(mode="json"),
                                         "matches": []},
           "current_hash": resource["content_hash"], "source_id": resource["source_id"],
           "external_id": resource["external_id"], "source_title": resource["title"],
           "source_url": resource["url"], "source_kind": resource["kind"],
           "tags": resource["tags"], "attributes": resource["attributes"]}
    monkeypatch.setattr(batch_review, "_pending", lambda *_args: (0, [row]))
    monkeypatch.setattr(batch_review, "_subjects", lambda _catalog: [])
    monkeypatch.setattr(batch_review, "_close_stale", lambda _catalog: 0)
    monkeypatch.setattr(batch_review, "judge", lambda items, **_: {
        items[0]["id"]: {"decision": "APPROVE", "reason": "原生场次与售票窗口一致"}})
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.setenv(name, "https://api.deepseek.com" if name == "LLM_BASE_URL" else "test")
    catalog = Mock()
    catalog.approve_review.return_value = True
    result = batch_review.run(catalog, apply=True)
    assert result["published"] == 1 and result["manual"] == 0
    assert catalog.approve_review.call_args.args[:3] == (
        "jpop-pia", "ai:deepseek-batch-v2", True)


def test_pia_jpop_artist_heading_cannot_auto_publish():
    row = candidate_row()
    row["attributes"] = {"source_type": "pia_ticket", "ticket_page": {
        "platform": "pia", "discoveryScope": "jpop", "events": []}}
    row["payload"]["activity"]["subject_slugs"] = []
    row["payload"]["activity"]["evidence"]["method"] = "structured"
    allowed, reason = batch_review.hard_gate(row, [])
    assert not allowed and "正式演出名" in reason


def test_editorial_and_community_need_source_backed_authority():
    row = candidate_row()
    row["source_url"] = "https://spice.eplus.jp/articles/123"
    row["payload"]["activity"]["url"] = "https://eplus.jp/sf/detail/123"
    row["attributes"] = {"source_type": "aggregator", "source_role": "editorial",
                         "outbound_links": [{"url": "https://eplus.jp/sf/detail/123"}]}
    assert batch_review.hard_gate(row)[0] is True
    row["attributes"]["outbound_links"] = []
    assert batch_review.hard_gate(row)[0] is False
    row["attributes"]["outbound_links"] = [{"url": "https://eplus.jp/sf/detail/123"}]
    row["attributes"]["source_role"] = "community"
    assert batch_review.hard_gate(row)[0] is False
    row["payload"]["matches"] = [{"strength": "exact_event_and_dates"}]
    assert batch_review.hard_gate(row)[0] is True


def test_music_news_scope_allows_unlisted_artist_only_with_event_evidence():
    row = candidate_row()
    row["source_url"] = "https://spice.eplus.jp/articles/123"
    row["tags"] = ["scope:anime-music-offline", "country:JP"]
    row["attributes"] = {"source_type": "aggregator", "source_role": "editorial",
                         "outbound_links": [{"url": "https://eplus.jp/sf/detail/123"}]}
    row["payload"]["activity"].update(
        title="架空歌手 東京公演", subject_slugs=[], venue="テストホール",
        url="https://eplus.jp/sf/detail/123")
    assert batch_review._model_item(row)["source"]["discovery_scope"] == "music"
    assert batch_review.hard_gate(row)[0] is True
    assert not batch_review._safe_out_of_scope(row, [])
    row["attributes"]["outbound_links"] = []
    assert batch_review.hard_gate(row)[0] is False
    row["attributes"]["outbound_links"] = [{"url": "https://eplus.jp/sf/detail/123"}]
    row["payload"]["activity"]["venue"] = None
    assert batch_review.hard_gate(row)[0] is False


def test_music_scope_requires_configured_japanese_source_role():
    row = candidate_row()
    row["payload"]["activity"]["subject_slugs"] = []
    row["tags"] = ["scope:music-offline", "country:JP"]
    row["attributes"] = {"source_type": "aggregator", "source_role": "editorial"}
    assert batch_review._model_item(row)["source"]["discovery_scope"] == "music"
    row["tags"] = ["scope:music-offline"]
    assert batch_review._model_item(row)["source"]["discovery_scope"] == "catalog"
    row["tags"] = ["scope:music-offline", "country:JP"]
    row["attributes"]["source_role"] = "unknown"
    assert batch_review._model_item(row)["source"]["discovery_scope"] == "catalog"


def test_music_news_extraction_prompt_does_not_require_anime_subject(monkeypatch):
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.setenv(name, "https://api.deepseek.com" if name == "LLM_BASE_URL" else "test")
    response = Mock(status_code=200)
    response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {
        "content": '{"activities":[]}'}}]}
    post = Mock(return_value=response)
    monkeypatch.setattr(batch_review.requests, "post", post)
    resource = {"id": 1, "source_id": "livefans-news", "external_id": "1",
                "content_hash": "hash-1", "content": "架空歌手 東京公演",
                "tags": ["scope:music-offline", "country:JP"],
                "attributes": {"source_role": "editorial"}}
    assert extract_text(resource, []) == []
    prompt = post.call_args.kwargs["json"]["messages"][1]["content"]
    assert "need not match the anime subject catalog" in prompt


def test_judge_rejects_missing_or_duplicate_results(monkeypatch):
    items = [batch_review._model_item(candidate_row("one")),
             batch_review._model_item(candidate_row("two"))]
    response = Mock(status_code=200)
    response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"reviews": [
            {"id": "one", "decision": "APPROVE", "reason": "证据完整"},
            {"id": "two", "decision": "MANUAL", "reason": "场次不清楚"},
        ]})}}]}
    post = Mock(return_value=response)
    monkeypatch.setattr(batch_review.requests, "post", post)
    result = batch_review.judge(items, subjects=[], key="test",
                                base="https://api.deepseek.com", model="test-model")
    assert result["one"]["decision"] == "APPROVE"
    assert len(post.call_args.kwargs["json"]["messages"][1]["content"]) > 100
    assert post.call_args.kwargs["json"]["thinking"] == {"type": "disabled"}
    response.json.return_value["choices"][0]["message"]["content"] = json.dumps({
        "reviews": [{"id": "one", "decision": "APPROVE", "reason": "证据完整"}]})
    with pytest.raises(ValueError, match="逐项"):
        batch_review.judge(items, subjects=[], key="test",
                           base="https://api.deepseek.com", model="test-model")


def test_review_run_batches_and_persists_only_high_confidence(monkeypatch):
    rows = [candidate_row("one"), candidate_row("two"), candidate_row("three")]
    rows[1]["attributes"]["source_role"] = "community"
    monkeypatch.setattr(batch_review, "_pending", lambda *_args: (7, rows))
    monkeypatch.setattr(batch_review, "_close_stale", lambda _catalog: 7)
    monkeypatch.setattr(batch_review, "_subjects", lambda _catalog: [])
    monkeypatch.setattr(batch_review, "_save_audit", lambda _catalog, _row, _audit: True)
    monkeypatch.setattr(batch_review, "judge", lambda items, **_: {
        item["id"]: {"decision": "APPROVE", "reason": "证据充分"} for item in items})
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.setenv(name, "https://api.deepseek.com" if name == "LLM_BASE_URL" else "test")
    catalog = Mock()
    catalog.approve_review.return_value = True
    result = batch_review.run(catalog, limit=3, batch_size=2, apply=True)
    assert {key: value for key, value in result.items() if key != "examples"} == {
        "stale": 7, "selected": 3, "batches": 2, "model_calls": 2,
        "published": 2, "out_of_scope": 0, "manual": 1, "contradicted": 0,
        "skipped": 0, "stale_closed": 7}
    assert len(result["examples"]) == 3
    assert catalog.approve_review.call_count == 2
    assert catalog.approve_review.call_args.kwargs["evidence_method"].startswith("llm:review:")


def test_plan_does_not_call_model(monkeypatch):
    monkeypatch.setattr(batch_review, "_pending", lambda *_args: (2, [candidate_row()]))
    monkeypatch.setattr(batch_review, "judge", lambda *_a, **_k: pytest.fail("model called"))
    assert batch_review.run(Mock())["selected"] == 1


def test_incomplete_model_batch_splits_then_keeps_single_failure_manual(monkeypatch):
    calls = []

    def fake_judge(items, **_):
        calls.append(len(items))
        if len(items) > 1 or items[0]["id"] == "two":
            raise ValueError("incomplete")
        return {items[0]["id"]: {"decision": "APPROVE", "reason": "证据充分"}}

    monkeypatch.setattr(batch_review, "judge", fake_judge)
    items = [{"id": "one"}, {"id": "two"}]
    decisions, count = batch_review._judge_bounded(
        items, subjects=[], key="test", base="https://api.deepseek.com", model="test")
    assert calls == [2, 1, 1] and count == 3
    assert decisions["one"]["decision"] == "APPROVE"
    assert decisions["two"]["decision"] == "MANUAL"


def test_out_of_scope_requires_no_known_subject(monkeypatch):
    subjects = [{"slug": "gakumas", "name": "学園アイドルマスター",
                 "name_zh": "学园偶像大师", "aliases": ["学マス"]}]
    row = candidate_row()
    row["payload"]["activity"]["subject_slugs"] = []
    assert not batch_review._safe_out_of_scope(row, subjects)
    row["payload"]["activity"]["title"] = "別の作品 東京公演"
    row["payload"]["activity"]["evidence"]["excerpt"] = "別の作品 東京公演"
    row["payload"]["activity"]["milestones"][0]["evidence"]["excerpt"] = "別の作品 東京公演"
    assert batch_review._safe_out_of_scope(row, subjects)
    row["attributes"] = {"source_type": "eplus_ticket",
                         "eplus_ticket": {"discoveryScope": "jpop"}}
    assert not batch_review._safe_out_of_scope(row, subjects)
    row["attributes"] = {"source_role": "official_operator"}
    monkeypatch.setattr(batch_review, "_pending", lambda *_args: (0, [row]))
    monkeypatch.setattr(batch_review, "_subjects", lambda _catalog: subjects)
    monkeypatch.setattr(batch_review, "_close_stale", lambda _catalog: 0)
    monkeypatch.setattr(batch_review, "judge", lambda items, **_: {
        item["id"]: {"decision": "OUT_OF_SCOPE", "reason": "无关联"} for item in items})
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.setenv(name, "https://api.deepseek.com" if name == "LLM_BASE_URL" else "test")
    catalog = Mock()
    catalog.approve_review.return_value = True
    result = batch_review.run(catalog, apply=True)
    assert result["out_of_scope"] == 1 and result["published"] == 0
    assert catalog.approve_review.call_args.args[2] is False


def test_source_type_filter_is_validated_before_query():
    with pytest.raises(ValueError, match="source-type"):
        batch_review.run(Mock(), source_type="official_site' OR TRUE")
    with pytest.raises(ValueError, match="source-id"):
        batch_review.run(Mock(), source_id="pia-jpop-tickets' OR TRUE")


def test_official_source_content_is_loaded_only_for_active_batch(monkeypatch):
    row = candidate_row()
    row["resource_id"] = 42
    row.pop("content")
    monkeypatch.setattr(batch_review, "_pending", lambda *_args: (0, [row]))
    monkeypatch.setattr(batch_review, "_subjects", lambda _catalog: [])
    monkeypatch.setattr(batch_review, "_close_stale", lambda _catalog: 0)
    monkeypatch.setattr(batch_review, "judge", lambda items, **_: {
        items[0]["id"]: {"decision": "APPROVE", "reason": "证据充分"}})
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.setenv(name, "https://api.deepseek.com" if name == "LLM_BASE_URL" else "test")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, query, params):
            assert "id=ANY" in query and params == ([42],)
            return Mock(fetchall=lambda: [{"id": 42, "content": QUOTE}])

    catalog = Mock()
    catalog.connect.return_value = Connection()
    catalog.approve_review.return_value = True
    assert batch_review.run(catalog, apply=True)["published"] == 1
