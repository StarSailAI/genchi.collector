from __future__ import annotations

import json

import pytest
import requests
from genchi_normalizer.app import (
    Normalizer,
    Settings,
    _asobi_match_acts,
    _asobi_real_acts,
    _asobi_ticket_phase,
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
    content = json.dumps(
        {"titleZh": None, "summaryZh": None, "category": "OTHER", "facts": []}
    )
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

    with pytest.raises(RuntimeError, match=r'LLM HTTP 400.*bad \[REDACTED\] request'):
        Normalizer(settings("https://api.deepseek.com", "deepseek-chat")).call_llm(
            {"url": "https://example.com", "title": "News", "content": "Body"}, "OTHER"
        )


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
    resource = {
        "attributes": {
            "asobi_ticket": {"acts": [direct], "included": [direct, other]}
        }
    }

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
        "attributes": {
            "asobi_ticket": {"acts": [pass_act], "included": [pass_act, real_act]}
        }
    }

    assert [act["id"] for act in _asobi_real_acts(resource)] == ["osaka-1"]
