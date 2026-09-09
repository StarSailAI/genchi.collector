from __future__ import annotations

import json

import pytest
from allfeeds_sdk import FetchContext, FetchRequest, PermanentError, TransientError
from genchi_fetchers.aggregators import (
    AggregatorConfig,
    AggregatorFetcher,
    clean_url,
    parse_animate,
    parse_article,
    source_date,
)
from genchi_product.pipeline import _text_candidates, evidence_blocks


def config(**kwargs):
    return AggregatorConfig(start_urls=("https://publisher.test/list",), detail_pattern=r"https://publisher\.test/article/\d+",
                            content_selector="article", max_detail_pages=3, refresh_details_per_run=1, **kwargs)


ARTICLE = '''<h1>学園アイドルマスター カフェ新情報</h1><time class="published" datetime="2026-09-02"></time>
<p class="updated">2026-09-08更新</p><article><p>学園アイドルマスター カフェは2026年9月3日から9月27日開催。</p>
<p>先着予約は9月26日23:59まで受付中。</p><a href="https://official.test/cafe/2026?utm_source=publisher">公式サイト</a>
<aside> unrelated advertisement </aside><script>bad()</script></article>'''


def test_clean_body_keeps_original_dates_links_and_excludes_navigation():
    result = parse_article(ARTICLE, "https://publisher.test/article/1", config(published_selector="time.published", updated_selector=".updated"))
    assert result["published"].day == 2 and result["published_precision"] == "DATE"
    assert result["updated"].day == 8
    assert "unrelated" not in result["content"] and "bad()" not in result["content"]
    assert "https://official.test/cafe/2026" in result["content"]
    assert result["links"] == [{"label": "公式サイト", "url": "https://official.test/cafe/2026"}]
    assert clean_url("https://publisher.test/detail?id=10&utm_source=mail#part") == "https://publisher.test/detail?id=10"
    assert clean_url("https://publisher.test/detail?id=11") != clean_url("https://publisher.test/detail?id=10")


def test_modified_is_not_published_and_real_times_keep_offsets():
    result = parse_article(ARTICLE, "https://publisher.test/article/1", config(updated_selector=".updated"))
    assert result["published"] is None and result["published_precision"] == "TBD"
    assert source_date("2026年9月8日 18:30")[0].hour == 18
    assert source_date("2026-09-08T09:30:00Z")[0].utcoffset().total_seconds() == 0
    assert source_date("2026-02-31") == (None, "TBD")


def test_discovery_backlog_refresh_and_checkpoint_commit(monkeypatch):
    state, records, requested = {}, [], []
    context = FetchContext(emit_record=records.append, emit_asset=lambda *_: None,
                           load_checkpoint=lambda: state.copy(), save_checkpoint=lambda s: (state.clear(), state.update(s)),
                           secret_provider=lambda _: "test", logger=None)
    monkeypatch.setattr("genchi_fetchers.aggregators.SafeHttpClient.validate_url", lambda *_: None)
    monkeypatch.setattr("genchi_fetchers.aggregators.SafeHttpClient._allowed_by_robots", lambda *_: True)
    broken = set()

    def render(_self, url, **kwargs):
        requested.append(url)
        if url in broken:
            raise TransientError("temporary upstream error")
        if url.endswith("list"):
            return "".join(f'<a href="/article/{i}?utm_source=list">article</a>' for i in range(1, 6)), url
        return ARTICLE, url

    monkeypatch.setattr("genchi_fetchers.aggregators.BrowserClient.render", render)
    request = FetchRequest(task_id=1, source_id="test", operation="fetch", config=config().model_dump(), tags=())
    fetcher = AggregatorFetcher()
    fetcher.fetch(context, request)
    assert len(records) == 3 and state["pending"] == ["https://publisher.test/article/4", "https://publisher.test/article/5"]
    assert len({r.external_id for r in records}) == 3
    previous = state.copy()
    broken.add("https://publisher.test/article/5")
    report = fetcher.fetch(context, request)
    assert report.status == "partial" and len(report.details["page_errors"]) == 1
    assert state != previous and state["pending"] == ["https://publisher.test/article/5"]
    broken.clear()
    fetcher.fetch(context, request)
    assert state["pending"] == [] and len(state["tracked"]) == 5
    assert records[0].external_id == records[4].external_id  # unchanged refresh keeps the upstream ID
    monkeypatch.setattr("genchi_fetchers.aggregators.SafeHttpClient._allowed_by_robots", lambda *_: False)
    with pytest.raises(PermanentError):
        fetcher.fetch(context, request)


def test_animate_public_state_keeps_shop_dates_separate_from_cms_timestamps():
    data = []

    def encode(value):
        index = len(data)
        data.append(None)
        if isinstance(value, dict):
            value = {k: encode(v) for k, v in value.items()}
        elif isinstance(value, list):
            value = [encode(v) for v in value]
        data[index] = value
        return index

    encode({"slug": "ac123", "name": "作品A", "description": "コラボ開催決定！",
            "createdAt": "2025-01-01T00:00:00Z", "updatedAt": "2026-06-03T00:00:00Z",
            "image": {"url": "https://cdn.animatecafe.jp/menu.jpg"}, "eventContents": [],
            "eventShops": [{"startsAt": "2026-08-14T15:00:00Z", "endsAt": "2026-09-15T14:59:59Z",
                            "shop": {"name": "アニメイトカフェ 池袋", "address": "東京都豊島区", "openingHours": "11:00～20:00"}}]})
    article = parse_animate('<script id="__NUXT_DATA__" type="application/json">' + json.dumps(data) + '</script>', "https://www.animatecafe.jp/event/ac123")
    assert "開催開始日: 2026-08-15" in article["content"]
    assert "開催終了日: 2026-09-15" in article["content"]
    assert "11:00～20:00" in article["content"] and "2025-01-01" not in article["content"]
    assert article["published"] is None and article["image_details_pending"] is True


def test_model_cannot_invent_official_link_for_merge():
    resource = {"id": 1, "source_id": "publisher", "external_id": "1", "content_hash": "v1",
                "url": "https://publisher.test/article/1", "title": "学園アイドルマスター カフェ", "content": "カフェ開催。公式サイト [https://official.test/cafe/2026]",
                "attributes": {"source_type": "aggregator"}}
    payload = {"activities": [{"title": "学園アイドルマスター カフェ", "evidence": "カフェ開催。",
                              "official_url": "https://invented.test/event"}]}
    with pytest.raises(ValueError, match="官方链接"):
        _text_candidates(json.dumps(payload), resource, [])
    payload["activities"][0]["official_url"] = "https://official.test/cafe/2026"
    result = _text_candidates(json.dumps(payload), resource, [])
    assert len(result) == 1 and result[0].publication == "REVIEW" and not result[0].evidence.verified


def test_evidence_ids_preserve_literal_source_and_reject_unknown_blocks():
    text = "学園アイドルマスター\n" + "原文の説明。\n" * 300
    blocks = evidence_blocks(text)
    assert "".join(blocks.values()) == text
    resource = {"id": 1, "source_id": "publisher", "external_id": "1", "content_hash": "v1", "content": text}
    value = {"activities": [{"title": "学園アイドルマスター", "evidence_id": "B2"}]}
    result = _text_candidates(json.dumps(value), resource, [])
    assert result[0].evidence.excerpt == blocks["B2"]
    value["activities"][0]["evidence_id"] = "B999"
    with pytest.raises(ValueError, match="原文证据"):
        _text_candidates(json.dumps(value), resource, [])


def test_same_named_tour_sessions_keep_distinct_stable_review_identities():
    resource = {"id": 1, "source_id": "publisher", "external_id": "1", "content_hash": "v1",
                "content": "公演：9月19日、20日 東京。"}
    sessions = [{"title": "ツアー2026", "evidence_id": "B1", "city": "東京",
                 "time": {"precision": "DATE", "starts_on": day}}
                for day in ["2026-09-19", "2026-09-20"]]
    items = _text_candidates(json.dumps({"activities": sessions}), resource, [])
    reversed_items = _text_candidates(json.dumps({"activities": sessions[::-1]}), resource, [])
    assert items[0].source_key != items[1].source_key
    assert items[0].source_key == reversed_items[1].source_key
    assert items[0].activity_key == items[1].activity_key


def test_calendar_header_preserves_start_and_doors_without_estimated_end():
    text = "正式公演名\n開催日時\n2026-09-23 (水)\n時間\n開場 17:00 開演 18:30 終演 21:00\n※終演時間は目安\n開催場所\n東京会場"
    resource = {"id": 1, "source_id": "eventernote-events", "external_id": "1", "content_hash": "v1",
                "url": "https://www.eventernote.com/events/123", "title": "正式公演名", "content": text,
                "attributes": {"source_type": "aggregator"}}
    value = {"activities": [{"title": "正式公演名", "evidence_id": "B1",
                             "time": {"precision": "TIME", "starts_at": "2026-09-23T17:00:00+09:00", "ends_at": "2026-09-23T21:00:00+09:00"}}]}
    item = _text_candidates(json.dumps(value), resource, [])[0]
    assert item.time.starts_at.hour == 18 and item.time.starts_at.minute == 30
    assert item.time.ends_at is None
    assert {n.kind: n.time.starts_at.hour for n in item.milestones} == {"START": 18, "DOORS": 17}
    assert all(n.evidence.excerpt in text and not n.evidence.verified for n in item.milestones)
    # Prose on unrelated publishers must never be interpreted as this header.
    resource['url'] = "https://publisher.test/article/1"
    item = _text_candidates(json.dumps(value), resource, [])[0]
    assert item.time.starts_at.hour == 17 and not item.milestones


def test_single_real_ticket_link_is_used_when_article_has_no_event_homepage():
    resource = {"id": 1, "source_id": "publisher", "external_id": "1", "content_hash": "v1",
                "url": "https://spice.eplus.jp/articles/123", "content": "一次先行 [https://eplus.jp/sf/detail/123]",
                "attributes": {"source_type": "aggregator"}}
    value = {"activities": [{"title": "Festival 2026", "evidence_id": "B1", "milestones": [
        {"kind": "TICKET", "title": "一次先行", "evidence_id": "B1", "url": "https://eplus.jp/sf/detail/123"}]}]}
    assert _text_candidates(json.dumps(value), resource, [])[0].url == 'https://eplus.jp/sf/detail/123'


def test_explicit_performance_rows_cannot_be_lost_as_tbd():
    resource = {"id": 1, "source_id": "spice-news", "external_id": "1", "content_hash": "v1",
                "url": "https://spice.eplus.jp/articles/123", "content": "開催日時\n2026年11月20日(金) 17時開場 / 19時開演\n2026年11月21日(土) 16時開場 / 18時開演",
                "attributes": {"source_type": "aggregator"}}
    value = {"activities": [{"title": "ピアノリサイタル", "evidence_id": "B1"}]}
    with pytest.raises(ValueError, match="逐场保留"):
        _text_candidates(json.dumps(value), resource, [])
    value['activities'] = [{"title": "ピアノリサイタル", "evidence_id": "B1", "time": {"precision": "TIME", "starts_at": when}}
                           for when in ['2026-11-20T19:00:00+09:00', '2026-11-21T18:00:00+09:00']]
    items = _text_candidates(json.dumps(value), resource, [])
    assert len(items) == 2 and items[0].activity_key == items[1].activity_key


def test_unknown_curtain_time_never_uses_known_doors_time():
    resource = {"id": 1, "source_id": "eventernote-events", "external_id": "1", "content_hash": "v1",
                "url": "https://www.eventernote.com/events/123", "title": "公演", "content": "開催日時\n2026-09-23 (水)\n時間\n開場 17:30 開演 - 終演 20:00\n開催場所\n会場",
                "attributes": {"source_type": "aggregator"}}
    value = {"activities": [{"title": "公演", "evidence_id": "B1", "time": {"precision": "TIME", "starts_at": "2026-09-23T17:30:00+09:00"}}]}
    item = _text_candidates(json.dumps(value), resource, [])[0]
    assert item.time.precision == 'DATE'
    assert [n.kind for n in item.milestones] == ['DOORS']


def test_empty_discovery_is_failure_and_next_page_is_processed(monkeypatch):
    records, state = [], {}
    context = FetchContext(emit_record=records.append, emit_asset=lambda *_: None,
                           load_checkpoint=lambda: state, save_checkpoint=state.update,
                           secret_provider=lambda _: "test", logger=None)
    monkeypatch.setattr("genchi_fetchers.aggregators.SafeHttpClient.validate_url", lambda *_: None)
    monkeypatch.setattr("genchi_fetchers.aggregators.SafeHttpClient._allowed_by_robots", lambda *_: True)
    pages = {'https://publisher.test/list': '<a class="next" href="/list?page=2">next</a>',
             'https://publisher.test/list?page=2': '<a href="/article/1">article</a>',
             'https://publisher.test/article/1': ARTICLE}
    monkeypatch.setattr("genchi_fetchers.aggregators.BrowserClient.render", lambda _, u, **kw: (pages[u], u))
    request = FetchRequest(task_id=1, source_id="test", operation="fetch", config=config(pages_per_root=2, next_selector='a.next').model_dump(), tags=())
    report = AggregatorFetcher().fetch(context, request)
    assert report.details['list_pages'] == 2 and len(records) == 1
    pages['https://publisher.test/list?page=2'] = '<p>No articles loaded</p>'
    with pytest.raises(TransientError, match='no articles'):
        AggregatorFetcher().fetch(context, request)
    assert AggregatorFetcher.manifest.operations == ('fetch',)
