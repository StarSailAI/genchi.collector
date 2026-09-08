from __future__ import annotations

import json

import pytest
import requests
from genchi_normalizer.app import (
    Normalizer,
    Settings,
    _activity_fingerprint,
    _asobi_match_acts,
    _asobi_real_acts,
    _asobi_ticket_phase,
    _ticket_relevance_rules,
)


class FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self) -> dict:
        return self._payload


def settings(base_url: str, model: str = "test-model") -> Settings:
    return Settings(
        database_url="postgresql://unused",
        schema="genchi",
        poll_seconds=2,
        llm_base_url=base_url,
        llm_api_key="secret-key",
        llm_model=model,
        llm_response_format="auto",
    )


def successful_response() -> FakeResponse:
    content = json.dumps({"titleZh": None, "summaryZh": None, "category": "OTHER", "facts": []})
    return FakeResponse(
        payload={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    )


def test_deepseek_uses_json_object(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return successful_response()

    monkeypatch.setattr(requests, "post", post)
    result = Normalizer(settings("https://api.deepseek.com", "deepseek-chat")).call_llm(
        {"url": "https://example.com/news", "title": "News", "content": "Body"}, "OTHER"
    )

    assert result["facts"] == []
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["json"]["response_format"] == {"type": "json_object"}
    assert "JSON object" in captured["json"]["messages"][1]["content"]


def test_other_provider_uses_json_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def post(_url, **kwargs):
        captured.update(kwargs)
        return successful_response()

    monkeypatch.setattr(requests, "post", post)
    Normalizer(settings("https://api.example.com/v1")).call_llm(
        {"url": "https://example.com/news", "title": "News", "content": "Body"}, "OTHER"
    )

    assert captured["json"]["response_format"]["type"] == "json_schema"


def test_http_error_keeps_detail_but_redacts_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(
            status_code=400, text='{"error":"bad secret-key request"}'
        ),
    )

    with pytest.raises(RuntimeError, match=r"LLM HTTP 400.*bad \[REDACTED\] request"):
        Normalizer(settings("https://api.deepseek.com", "deepseek-chat")).call_llm(
            {"url": "https://example.com", "title": "News", "content": "Body"}, "OTHER"
        )


def ticket_resource(
    title: str,
    *,
    native_categories: list[str] | None = None,
    matched_keywords: list[str] | None = None,
    discovery: list[dict] | None = None,
    url: str = "https://l-tike.com/search/?keyword=%E5%A3%B0%E5%84%AA",
) -> dict:
    return {
        "url": url,
        "title": title,
        "content": (
            f"イベント: {title}\nジャンル: {(native_categories or ['演劇・ステージ・舞台'])[0]}"
        ),
        "attributes": {
            "source_type": "lawson_ticket",
            "ticket_page": {
                "pageId": "test",
                "nativeCategories": native_categories or [],
                "matchedKeywords": matched_keywords or [],
                "discovery": discovery or [],
                "events": [],
            },
        },
        "tags": ["project:unknown"],
    }


def test_ticket_relevance_keeps_native_labels_as_llm_context() -> None:
    anime = _ticket_relevance_rules(
        ticket_resource(
            "アニメイベント",
            native_categories=["アニメ･ゲーム", "アニメ･声優イベント"],
        ),
        platform="eplus",
    )
    rejected = _ticket_relevance_rules(
        ticket_resource("一般競技大会", native_categories=["スポーツ"]),
        platform="lawson",
    )

    assert anime["status"] == "review"
    assert anime["method"] == "structured-gate"
    assert rejected["status"] == "review"


def test_ticket_relevance_keeps_generic_lawson_stage_in_review() -> None:
    decision = _ticket_relevance_rules(
        ticket_resource("ＢＱＭＡＰ３５周年記念公演『源内人形』"),
        platform="lawson",
    )

    assert decision["status"] == "review"
    assert "search-query:声優" in decision["signals"]


def test_ticket_relevance_accepts_trusted_category_without_project_enumeration() -> None:
    decision = _ticket_relevance_rules(
        ticket_resource(
            "新作イベント",
            discovery=[
                {
                    "kind": "platform_category",
                    "sourceUrl": "https://t.pia.jp/anime/",
                    "trustedCategory": True,
                }
            ],
        ),
        platform="pia",
    )

    assert decision["status"] == "accepted"
    assert decision["confidence"] == 0.99


def test_ticket_relevance_llm_treats_search_query_as_context_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    content = json.dumps(
        {
            "isRelevant": False,
            "confidence": 0.98,
            "reason": "普通の舞台公演で、二次元作品との関係が確認できない",
            "subjectName": "源内人形",
            "subjectType": "UNKNOWN",
            "canonicalTitle": "源内人形",
            "shortTitle": "源内人形",
            "projectName": None,
            "eventType": "OTHER",
            "works": [],
            "performers": [],
            "venues": [],
            "sessionTimes": [],
            "ticketPhases": [],
            "evidence": ["ＢＱＭＡＰ３５周年記念公演"],
        }
    )

    def post(_url, **kwargs):
        captured.update(kwargs)
        return FakeResponse(
            payload={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        )

    monkeypatch.setattr(requests, "post", post)
    resource = ticket_resource("ＢＱＭＡＰ３５周年記念公演『源内人形』")
    rule = _ticket_relevance_rules(resource, platform="lawson")
    result = Normalizer(
        settings("https://api.deepseek.com", "deepseek-chat")
    ).call_ticket_relevance_llm(resource, platform="lawson", rule_decision=rule)

    assert result["status"] == "rejected"
    assert result["confidence"] == 0.98
    prompt = captured["json"]["messages"][1]["content"]
    assert "discovery context only" in prompt
    assert "源内人形" in prompt


def test_ticket_relevance_requires_high_confidence_for_auto_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = json.dumps(
        {
            "isRelevant": True,
            "confidence": 0.94,
            "reason": "声優イベントと考えられるが明示的な作品関係は弱い",
            "subjectName": "Example",
            "subjectType": "VOICE_ACTOR",
            "canonicalTitle": "Example Live",
            "shortTitle": "Example Live",
            "projectName": None,
            "eventType": "LIVE",
            "works": [],
            "performers": ["Example"],
            "venues": [],
            "sessionTimes": [],
            "ticketPhases": ["GENERAL"],
            "evidence": ["Example Live"],
        }
    )
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(
            payload={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        ),
    )
    resource = ticket_resource("Example Live")
    decision = Normalizer(
        settings("https://api.deepseek.com", "deepseek-chat")
    ).call_ticket_relevance_llm(
        resource,
        platform="lawson",
        rule_decision=_ticket_relevance_rules(resource, platform="lawson"),
    )

    assert decision["status"] == "review"
    assert decision["shortTitle"] == "Example Live"


def test_activity_fingerprint_ignores_width_spacing_and_punctuation() -> None:
    assert _activity_fingerprint("Ｌｏｖｅ　Ｌｉｖｅ！") == _activity_fingerprint("Love Live")


def test_asobi_match_acts_uses_date_and_venue() -> None:
    acts = [
        {
            "id": "osaka-1",
            "attributes": {
                "name": "DAY 1 Zepp Namba(OSAKA)",
                "venue": "Zepp Namba(OSAKA)",
                "performance_date": "2026-08-06",
            },
        },
        {
            "id": "osaka-2",
            "attributes": {
                "name": "DAY 2 Zepp Namba(OSAKA)",
                "venue": "Zepp Namba(OSAKA)",
                "performance_date": "2026-08-07",
            },
        },
        {
            "id": "tokyo-1",
            "attributes": {
                "name": "DAY 1 Zepp Haneda(TOKYO)",
                "venue": "Zepp Haneda(TOKYO)",
                "performance_date": "2026-08-27",
            },
        },
    ]

    matched = _asobi_match_acts("【Zepp Namba(OSAKA)公演】8月6日・7日 一般発売", acts)

    assert [act["id"] for act in matched] == ["osaka-1", "osaka-2"]
    assert _asobi_ticket_phase("resale_lottery", "リセール") == "RESALE"
    assert _asobi_ticket_phase("lottery", "プレミアム会員先行") == "FC_PRE"


def test_asobi_match_acts_prioritizes_specific_date_over_shared_venue() -> None:
    acts = [
        {
            "id": "osaka-1",
            "attributes": {
                "name": "DAY 1 Zepp Namba(OSAKA)",
                "venue": "Zepp Namba(OSAKA)",
                "performance_date": "2026-08-06",
            },
        },
        {
            "id": "osaka-2",
            "attributes": {
                "name": "DAY 2 Zepp Namba(OSAKA)",
                "venue": "Zepp Namba(OSAKA)",
                "performance_date": "2026-08-07",
            },
        },
    ]

    matched = _asobi_match_acts("【Zepp Namba(OSAKA)公演】リセール（8月6日(木)公演）", acts)

    assert [act["id"] for act in matched] == ["osaka-1"]


def test_asobi_real_acts_prefers_direct_reception_relationship() -> None:
    direct = {
        "type": "act",
        "id": "osaka-1",
        "attributes": {"name": "8月6日公演"},
    }
    other = {
        "type": "act",
        "id": "osaka-2",
        "attributes": {"name": "8月7日公演"},
    }
    resource = {"attributes": {"asobi_ticket": {"acts": [direct], "included": [direct, other]}}}

    assert [act["id"] for act in _asobi_real_acts(resource)] == ["osaka-1"]


def test_asobi_real_acts_falls_back_for_multi_day_pass_pseudo_act() -> None:
    pass_act = {
        "type": "act",
        "id": "pass",
        "attributes": {"name": "2日間通し券"},
    }
    real_act = {
        "type": "act",
        "id": "osaka-1",
        "attributes": {"name": "8月6日公演"},
    }
    resource = {
        "attributes": {"asobi_ticket": {"acts": [pass_act], "included": [pass_act, real_act]}}
    }

    assert [act["id"] for act in _asobi_real_acts(resource)] == ["osaka-1"]


@pytest.mark.parametrize("schema_name", ["genchi_enrichment", "genchi_ticket_relevance"])
def test_every_legacy_model_call_includes_shared_glossary(monkeypatch, schema_name):
    from genchi_normalizer.glossary import load_glossary

    captured = {}

    def post(_url, **kwargs):
        captured.update(kwargs)
        return successful_response()

    monkeypatch.setattr(requests, "post", post)
    Normalizer(settings("https://api.example.test/v1"))._call_llm_json(
        schema_name=schema_name,
        schema={"type": "object"},
        prompt="Return JSON",
        system_prompt="Extract facts.",
        max_tokens=4096,
    )
    system = captured["json"]["messages"][0]["content"]
    assert load_glossary()["version"] in system
    assert "アソビストア" in system and "ASOBI STORE" in system
    assert "animate" in system and "一般贩售" in system and "事前贩售" in system
