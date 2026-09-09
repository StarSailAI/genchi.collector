from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from allfeeds_builtin.http import SafeHttpClient
from allfeeds_control.config import load_sources
from allfeeds_control.scheduling import next_run
from allfeeds_sdk import (
    AuthenticationError,
    FetchContext,
    FetchRequest,
    RateLimitError,
    TransientError,
    UpstreamHTTPError,
)
from bs4 import BeautifulSoup
from genchi_fetchers.fetchers import (
    BrowserClient,
    EplusTicketFetcher,
    LawsonTicketFetcher,
    OfficialSiteFetcher,
    PiaTicketFetcher,
    _eplus_jst,
    _lawson_parse_results,
    _pia_performances,
    _select_ticket_details,
)
from genchi_product.pipeline import _source_excerpt, extract_text, precise


def context(records, state):
    return FetchContext(
        emit_record=records.append,
        emit_asset=lambda *_: None,
        load_checkpoint=lambda: dict(state),
        save_checkpoint=lambda value: (state.clear(), state.update(value)),
        secret_provider=lambda _: "test",
        logger=None,
    )


def test_http_404_is_typed_without_logging_query_credentials(monkeypatch):
    client = SafeHttpClient(
        user_agent="test", timeout_seconds=1, retries=0, max_response_bytes=1024,
        obey_robots=False, allow_private_network=False,
        allowed_hosts=("example.test",), rate_limit_seconds=0,
    )
    url = "https://example.test/expired?token=not-for-logs"
    monkeypatch.setattr(client.session, "get", lambda *_a, **_k: SimpleNamespace(status_code=404, url=url))
    with pytest.raises(UpstreamHTTPError) as error:
        client.get(url)
    assert error.value.status_code == 404
    assert "not-for-logs" not in str(error.value)


def test_browser_retries_transient_page_failure_but_not_bad_auth(monkeypatch):
    responses = iter([
        SimpleNamespace(status_code=502),
        SimpleNamespace(status_code=200, headers={}, text="article"),
        SimpleNamespace(status_code=401),
    ])
    delays = []
    monkeypatch.setattr("genchi_fetchers.fetchers.requests.post", lambda *_a, **_k: next(responses))
    monkeypatch.setattr("genchi_fetchers.fetchers.time.sleep", delays.append)
    client = BrowserClient("http://browser:3003", "test")
    assert client.render("https://example.test/news") == ("article", "https://example.test/news")
    assert delays == [2]
    with pytest.raises(AuthenticationError):
        client.render("https://example.test/news")
    assert delays == [2]


def test_browser_rate_limit_waits_then_returns_to_scheduler(monkeypatch):
    monkeypatch.setattr("genchi_fetchers.fetchers.requests.post", lambda *_a, **_k: SimpleNamespace(
        status_code=200, headers={"X-Genchi-Browser-Upstream-Status": "429"},
    ))
    delays = []
    monkeypatch.setattr("genchi_fetchers.fetchers.time.sleep", delays.append)
    with pytest.raises(RateLimitError) as error:
        BrowserClient("http://browser:3003", "test").render("https://example.test/news")
    assert error.value.retry_after_seconds == 60
    assert delays == [60, 120]


@pytest.mark.parametrize("retry_after,expected_waits", [("90", [90]), ("600", [])])
def test_browser_honors_upstream_cooldown_without_replaying_completed_pages(monkeypatch, retry_after, expected_waits):
    replies = iter([
        SimpleNamespace(status_code=200, headers={"X-Genchi-Browser-Upstream-Status": "429", "Retry-After": retry_after}),
        SimpleNamespace(status_code=200, headers={}, text="recovered"),
    ])
    delays = []
    monkeypatch.setattr("genchi_fetchers.fetchers.requests.post", lambda *_a, **_k: next(replies))
    monkeypatch.setattr("genchi_fetchers.fetchers.time.sleep", delays.append)
    browser = BrowserClient("http://browser:3003", "test")
    if expected_waits:
        assert browser.render("https://example.test/tickets")[0] == "recovered"
        assert browser.rate_limit_retries == 1
    else:
        with pytest.raises(RateLimitError) as error:
            browser.render("https://example.test/tickets")
        assert error.value.retry_after_seconds == 600
    assert delays == expected_waits


@pytest.mark.parametrize("fetcher", [EplusTicketFetcher, PiaTicketFetcher, LawsonTicketFetcher])
def test_ticket_source_does_not_hide_rate_limit_in_partial_report_or_browser_fallback(monkeypatch, fetcher):
    calls = []

    def limited(*_args, **_kwargs):
        calls.append(1)
        raise RateLimitError("upstream 429", retry_after_seconds=60)

    monkeypatch.setattr(SafeHttpClient, "get", limited)
    monkeypatch.setattr(BrowserClient, "render", limited)
    with pytest.raises(RateLimitError):
        fetcher().fetch(context([], {}), FetchRequest(task_id=1, source_id="ticket", operation="fetch", tags=(), config={}))
    assert len(calls) == 1


@pytest.mark.parametrize("status", [404, 410, 403])
def test_expired_ticket_does_not_lose_other_events_or_retry_forever(monkeypatch, status):
    root = "https://eplus.jp/sf/anime/kanto"
    expired = "https://eplus.jp/sf/detail/4529710001"
    current = "https://eplus.jp/sf/detail/4512340001"
    event = {"@type": "Event", "name": "THE IDOLM@STER TEST LIVE", "startDate": "2026-10-01", "url": current}
    pages = {
        root: f'<a href="{current}">current</a>',
        current: '<script type="application/ld+json">' + json.dumps(event) + '</script>',
    }

    def get(_self, url, **_kwargs):
        if url == expired:
            raise UpstreamHTTPError(status, url)
        return SimpleNamespace(text=pages[url], url=url)

    monkeypatch.setattr(SafeHttpClient, "get", get)
    records, state = [], {"tracked_detail_urls": [expired]}
    request = FetchRequest(task_id=1, source_id="eplus-anime-tickets", operation="fetch", tags=(), config={
        "category_urls": [root], "pages_per_root": 1, "browser_fallback": False,
    })
    if status == 403:
        with pytest.raises(UpstreamHTTPError):
            EplusTicketFetcher().fetch(context(records, state), request)
        return
    report = EplusTicketFetcher().fetch(context(records, state), request)
    assert report.status == "succeeded"
    assert report.details["missing_details"] == [expired]
    assert [record.external_id for record in records] == ["eplus:detail:4512340001"]
    assert state["tracked_detail_urls"] == [current]
    assert records[0].attributes["eplus_ticket"]["events"][0]["startsAt"] == "2026-10-01"


def test_refresh_is_reserved_and_discovery_tail_is_visited():
    candidates = {str(i): f"https://example.test/{i}" for i in range(7)}
    discoveries = {"6": [{"kind": "refresh"}]}
    cursor, seen = 0, set()
    for _ in range(3):
        selected, cursor = _select_ticket_details(candidates, discoveries, 3, cursor)
        assert selected[0][0] == "6"
        seen.update(key for key, _ in selected)
    assert seen == set(candidates)


def test_official_heading_date_and_body_exclude_related_articles(monkeypatch):
    pages = {
        "https://example.test/news/": '<a href="/news/one">one</a>',
        "https://example.test/news/one": '''<main><h1 class="logo"></h1>
            <meta property="og:title" content="outdated social title">
            <div class="header"><h1>公式ライブ開催</h1><time>2026.<span>09.09</span></time></div>
            <div class="body">受付は9月10日から。</div><aside>RELATED: unrelated event</aside></main>''',
    }
    monkeypatch.setattr(SafeHttpClient, "get", lambda _s, url, **_k: SimpleNamespace(url=url, text=pages[url]))
    records = []
    OfficialSiteFetcher().fetch(context(records, {}), FetchRequest(
        task_id=1, source_id="news", operation="fetch", tags=(), config={
            "start_urls": ["https://example.test/news/"], "link_pattern": "/news/one$",
            "title_selector": ".header h1", "content_selector": ".body", "published_selector": "time",
        },
    ))
    row = records[0]
    assert row.title == "公式ライブ開催"
    assert row.published_at.isoformat() == "2026-09-09T00:00:00+09:00"
    assert row.attributes["published_precision"] == "DATE"
    assert row.content == "公式ライブ開催\n2026-09-09\n受付は9月10日から。"


@pytest.mark.parametrize("body,missing", [("お探しのページは見つかりませんでした", True), ("Access denied", False)])
def test_only_explicit_source_tombstone_can_retire_a_403_detail(monkeypatch, body, missing):
    def render(_self, url, **_kwargs):
        if url.endswith("/removed"):
            raise UpstreamHTTPError(403, url, response_text=body)
        if url.endswith("/current"):
            return "<h1>ライブ</h1><article>受付のお知らせ</article>", url
        return '<a href="/news/removed">removed</a><a href="/news/current">current</a>', url

    monkeypatch.setattr(BrowserClient, "render", render)
    request = FetchRequest(task_id=1, source_id="news", operation="fetch", tags=(), config={
        "browser": True, "start_urls": ["https://example.test/news/"], "link_pattern": "/news/(removed|current)$",
        "missing_detail_text": "お探しのページは見つかりませんでした",
    })
    records = []
    if not missing:
        with pytest.raises(UpstreamHTTPError):
            OfficialSiteFetcher().fetch(context(records, {}), request)
    else:
        report = OfficialSiteFetcher().fetch(context(records, {}), request)
        assert len(records) == 1
        assert records[0].title == "ライブ"
        assert report.details["missing_details"] == [{"url": "https://example.test/news/removed", "http_status": 403}]


def test_date_only_ticket_keeps_precision_and_existing_fallback_identity():
    assert _eplus_jst("2026-10-01") == "2026-10-01"
    html = '<div class="Y15-regular-section"><span class="Y15-event-date">2026/10/01</span></div>'
    row = _pia_performances(BeautifulSoup(html, "lxml"), title="展覧会", page_url="https://t.pia.jp/", window=None)[0]
    previous_id = hashlib.sha256("展覧会:2026-10-01T00:00:00+09:00:0".encode()).hexdigest()[:24]
    assert row["id"] == previous_id
    assert row["startsAt"] == "2026-10-01"
    assert precise(row["startsAt"]).precision == "DATE"
    assert precise("2026-10-01T17:00:00+09:00", "2026-10-02").ends_at is None


def test_evidence_whitespace_mapping_returns_original_span_and_rejects_changed_facts():
    text = "前文\n対象商品をお買上げ\n3,000円(税込)ごとに\nプレゼント！\n後文"
    proof = "対象商品をお買上げ3,000円(税込)ごとに、プレゼント！"
    assert _source_excerpt(text, proof) is None  # Added punctuation is not whitespace.
    quote = _source_excerpt(text, proof.replace("、", ""))
    assert quote == "対象商品をお買上げ\n3,000円(税込)ごとに\nプレゼント！"
    assert quote in text
    assert _source_excerpt(text, proof.replace("3,000", "5,000")) is None
    assert _source_excerpt("9月1日\n別の情報\n9月2日", "9月1日9月2日") is None
    assert _source_excerpt("受付\n開始。受付 開始", "受付開始") is None  # Ambiguous spans.


def test_lawson_period_pass_preserves_range_and_admission_conditions():
    html = '''<div class="ResultBox"><h3 class="ResultBox__title">展覧会</h3>
      <div class="ResultBox__information"><dt class="ResultBox__informationTitle">公演日：</dt>
      <dd class="ResultBox__informationText">2026/9/12(土)～2026/9/30(水)</dd></div>
      <div class="ResultBox__table prfItem"><span id="sale_name">平日期間有効券（土日祝は入場不可）</span>
      <span id="receiptDat">2026/8/27(木) 10:00～2026/9/29(火) 18:00</span>
      <a class="entryBtn" data-lcode="12345" data-prfdate="" data-pfkeys="native-pass-id">詳細</a></div></div>'''
    result = _lawson_parse_results(html, page_url="https://l-tike.com/search/", search_query="展覧会", project_keywords={})
    events = result[0]["events"]
    assert len(events) == 1
    assert events[0]["startsAt"] == "2026-09-12"
    assert events[0]["endsAt"] == "2026-09-30"
    assert "土日祝は入場不可" in events[0]["ticketWindows"][0]["notes"]
    assert events[0]["ticketWindows"][0]["nativePerformanceKeys"] == ["native-pass-id"]


@pytest.mark.parametrize("known_empty", [True, False])
def test_lawson_empty_search_requires_the_official_empty_state(monkeypatch, known_empty):
    def render(_browser, url, **kwargs):
        assert "#navSearchCount.NoResult" in kwargs["selector"]
        return ('<div id="navSearchCount" class="NoResult"><p>条件に一致するチケットは見つかりませんでした。</p></div>'
                if known_empty else '<div id="layout_search_result">Loading...</div>'), url

    monkeypatch.setattr(BrowserClient, "render", render)
    records, state = [], {}
    request = FetchRequest(task_id=1, source_id="lawson", operation="fetch", tags=(), config={
        "search_keywords": ["作品名"], "queries_per_run": 1,
    })
    if known_empty:
        result = LawsonTicketFetcher().fetch(context(records, state), request)
        assert result.status == "succeeded"
        assert result.details["search_pages"] == 1
        assert result.details["empty_queries"] == ["作品名"]
        assert result.details["results"] == 0
    else:
        with pytest.raises(TransientError, match="neither results nor an explicit empty state"):
            LawsonTicketFetcher().fetch(context(records, state), request)
        assert state == {}
    assert records == []


def test_lawson_native_round_separates_pass_from_dated_ticket_and_survives_deadline_change():
    def parse(deadline):
        html = '''<div class="ResultBox"><h3 class="ResultBox__title">展覧会</h3>
          <div class="ResultBox__information"><dt class="ResultBox__informationTitle">公演日：</dt>
          <dd class="ResultBox__informationText">2026/9/12(土)～2026/9/30(水)</dd></div>'''
        for schedule, dates, end in [("1", "", "2026/9/29(火) 18:00"), ("2", "20260912", deadline)]:
            html += f'''<div class="ResultBox__table prfItem"><span id="sale_name">一般発売</span>
              <span id="receiptDat">2026/8/27(木) 10:00～{end}</span>
              <a class="entryBtn" data-lcode="12345" data-rcptypename="02" data-schduleno="{schedule}"
              data-prfdate="{dates}">詳細</a></div>'''
        return _lawson_parse_results(html + '</div>', page_url="https://l-tike.com/search/", search_query="展覧会", project_keywords={})[0]["events"]

    before = parse("2026/9/27(日) 19:00")
    after = parse("2026/9/28(月) 19:00")
    assert len(before) == 2
    assert before[0]["id"] != before[1]["id"]
    assert before[0]["endsAt"] == "2026-09-30"
    assert before[1]["endsAt"] is None
    windows = [event["ticketWindows"][0] for event in before]
    assert windows[0]["legacyId"] == windows[1]["legacyId"]
    assert windows[0]["id"] != windows[1]["id"]
    assert [w["id"] for w in windows] == [event["ticketWindows"][0]["id"] for event in after]


@pytest.mark.parametrize("host,expected", [("api.deepseek.com", True), ("model.example.test", False)])
def test_deepseek_output_budget_is_for_json_and_other_providers_stay_compatible(monkeypatch, host, expected):
    monkeypatch.setenv("LLM_API_KEY", "test")
    monkeypatch.setenv("LLM_BASE_URL", f"https://{host}/chat/completions")
    monkeypatch.setenv("LLM_MODEL", "test")
    captured = {}

    def post(_url, **kwargs):
        captured.update(kwargs["json"])
        return SimpleNamespace(status_code=200, json=lambda: {"choices": [{"finish_reason": "stop", "message": {"content": '{"activities":[]}'}}]})

    monkeypatch.setattr("genchi_product.pipeline.requests.post", post)
    assert extract_text({"content": "配信番組のお知らせ"}, []) == []
    assert ("thinking" in captured) is expected
    if expected:
        assert captured["thinking"] == {"type": "disabled"}


def test_all_sources_have_distinct_daily_schedules():
    sources = load_sources(Path("config/sources.yaml")).value.sources
    now = datetime(2026, 9, 9, 0, tzinfo=UTC)
    times = []
    for source in sources:
        first = next_run(source.schedule, now)
        second = next_run(source.schedule, first)
        assert second - first == timedelta(days=1)
        times.append(first)
    assert len(set(times)) == len(sources)


@pytest.mark.parametrize("repair_valid", [True, False])
def test_model_correction_is_bounded_and_keeps_evidence_and_review_gates(monkeypatch, repair_valid):
    monkeypatch.setenv("LLM_API_KEY", "test")
    monkeypatch.setenv("LLM_BASE_URL", "https://model.example.test/v1")
    monkeypatch.setenv("LLM_MODEL", "test")
    calls = []
    resource = {
        "id": 1, "source_id": "official", "external_id": "news:1", "content_hash": "hash",
        "url": "https://example.test/news/1", "title": "公式ライブ", "content": "公式ライブ\n2026年10月1日",
    }

    def post(_url, **kwargs):
        calls.append(json.loads(json.dumps(kwargs["json"])))
        proof = resource["content"] if len(calls) == 2 and repair_valid else "存在しない原文"
        item = {"title": "公式ライブ", "evidence": proof, "time": {"precision": "DATE", "starts_on": "2026-10-01"}}
        return SimpleNamespace(status_code=200, json=lambda: {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"activities": [item]}, ensure_ascii=False),
        }}]})

    monkeypatch.setattr("genchi_product.pipeline.requests.post", post)
    if repair_valid:
        items = extract_text(resource, [])
        assert items[0].publication == "REVIEW"
        assert items[0].evidence.verified is False
        assert items[0].evidence.excerpt == resource["content"]
        assert items[0].time.starts_at is None
    else:
        with pytest.raises(ValueError, match="缺少可定位"):
            extract_text(resource, [])
    assert len(calls) == 2
    assert len(calls[1]["messages"]) == 4
    assert calls[0]["messages"][0] == calls[1]["messages"][0]
