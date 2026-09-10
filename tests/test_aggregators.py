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


def test_discovery_source_page_cannot_become_the_official_activity_url():
    resource = {"id": 1, "source_id": "community", "external_id": "1", "content_hash": "v1",
                "url": "https://community.test/events/123", "content": "Festival 2026",
                "attributes": {"source_type": "aggregator", "source_role": "community"}}
    value = {"activities": [{"title": "Festival 2026", "evidence_id": "B1",
                              "official_url": "https://community.test/events/123"}]}
    assert _text_candidates(json.dumps(value), resource, [])[0].url is None

    resource["content"] += " [https://organizer.test/festival]"
    value["activities"][0]["official_url"] = "https://organizer.test/festival"
    assert _text_candidates(json.dumps(value), resource, [])[0].url == "https://organizer.test/festival"


@pytest.mark.parametrize('proof', [
    '受付期間：9/9(水) 12:00 ～ 9/23(水・祝) 23:59まで',
    '最速先行 2026年9月9日（水）12:00～9月23日（水）23:59',
])
def test_known_ticket_clocks_are_not_silently_downgraded_to_dates(proof):
    resource = {"id": 1, "source_id": "publisher", "external_id": "1", "content_hash": "v1",
                "url": "https://publisher.test/article/123", "content": proof,
                "attributes": {"source_type": "aggregator"}}
    node = {"kind": "TICKET", "title": "最速先行", "evidence_id": "B1",
            "time": {"precision": "DATE", "starts_on": "2026-09-09", "ends_on": "2026-09-23"}}
    value = {"activities": [{"title": "公演", "evidence_id": "B1", "milestones": [node]}]}
    with pytest.raises(ValueError, match='不可降为 DATE'):
        _text_candidates(json.dumps(value), resource, [])
    node['time'] = {'precision': 'TIME', 'starts_at': '2026-09-09T12:00:00+09:00', 'ends_at': '2026-09-23T23:59:00+09:00'}
    item = _text_candidates(json.dumps(value), resource, [])[0]
    assert item.milestones[0].time.starts_at.hour == 12
    assert item.milestones[0].time.ends_at.minute == 59
    assert item.publication == 'REVIEW' and not item.milestones[0].evidence.verified


@pytest.mark.parametrize('proof', [
    '受付期間 9/9～9/23、開演 19:00',  # Performance clock is not a ticket clock.
    '先行受付 9/9～9/23。別の受付 9/10 12:00～9/22 23:59',
    '前年の受付 2025年9月9日12:00～2025年9月23日23:59',
    '受付 9/9 12:00～9/23 時刻未定',  # Partial clock precision cannot fill an unknown end.
])
def test_date_only_window_does_not_borrow_other_dates_or_clocks(proof):
    resource = {"id": 1, "source_id": "publisher", "external_id": "1", "content_hash": "v1",
                "url": "https://publisher.test/article/123", "content": proof,
                "attributes": {"source_type": "aggregator"}}
    value = {"activities": [{"title": "公演", "evidence_id": "B1", "milestones": [
        {"kind": "TICKET", "title": "先行", "evidence_id": "B1",
         "time": {"precision": "DATE", "starts_on": "2026-09-09", "ends_on": "2026-09-23"}}]}]}
    assert _text_candidates(json.dumps(value), resource, [])[0].milestones[0].time.precision == 'DATE'


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


@pytest.mark.parametrize('section', ['comic', 'music'])
def test_natalie_source_keeps_event_evidence_and_excludes_recommendation_dates(section):
    from pathlib import Path

    import yaml
    sources = yaml.safe_load((Path(__file__).resolve().parents[1]/'config/sources.yaml').read_text())['sources']
    source = next(s for s in sources if s['id'] == f'natalie-{section}-news')
    cfg = AggregatorConfig.model_validate(source['config'])
    html = '''<article class="NA_article"><div class="NA_article_header">
    <h1 class="NA_article_title">テスト展覧会 新情報</h1><span class="NA_article_date">2026年9月9日 18:37</span></div>
    <div class="NA_article_body"><p>テスト展覧会は2026年10月1日から10月12日まで東京都の会場で開催します。</p>
    <h2>開催概要</h2><p>一般販売は9月15日10:00から受付。最終入場は17:00です。</p>
    <div class="NA_article_link"><p>リンク</p><a data-gtm-click="external_link" href="https://official.test/expo/">イベント公式</a>
    <a data-gtm-click="external_link" href="https://l-tike.com/event/mevent/?mid=12345">チケット</a></div>
    <div class="NA_article_embed_article">古い公演 2024年3月17日</div>
    <div class="NA_article_link"><p>関連記事</p><a href="/comic/news/123">無関係の展覧会 2024年4月1日</a></div>
    <div class="NA_article_prefsource">Google登録はこちら</div>
    <div class="NA_share"><a href="https://twitter.com/intent/tweet">シェア</a></div>
    <div class="NA_article_social">読者の反応</div></div></article>'''
    article = parse_article(html, f'https://natalie.mu/{section}/news/456', cfg)
    assert article['published'].isoformat() == '2026-09-09T18:37:00+09:00'
    assert article['published_precision'] == 'TIME' and article['updated'] is None
    assert '2026年10月1日' in article['content'] and '9月15日10:00' in article['content']
    assert '2024年' not in article['content'] and 'Google' not in article['content']
    assert 'シェア' not in article['content'] and '読者の反応' not in article['content']
    assert {x['url'] for x in article['links']} == {'https://official.test/expo/', 'https://l-tike.com/event/mevent/?mid=12345'}


def test_access_denial_stops_sweep_preserves_backlog_and_spaces_requests(monkeypatch):
    from allfeeds_sdk import UpstreamHTTPError

    state, records, requested, moments = {}, [], [], []
    clock = [0.0]
    monkeypatch.setattr('genchi_fetchers.aggregators.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('genchi_fetchers.aggregators.time.sleep', lambda delay: clock.__setitem__(0, clock[0]+delay))
    monkeypatch.setattr('genchi_fetchers.aggregators.SafeHttpClient.validate_url', lambda *_: None)
    monkeypatch.setattr('genchi_fetchers.aggregators.SafeHttpClient._allowed_by_robots', lambda *_: True)
    def render(_, url, **kwargs):
        requested.append(url)
        moments.append(clock[0])
        if url.endswith('list'):
            return ''.join(f'<a href="/article/{n}">news</a>' for n in (1, 2, 3)), url
        if url.endswith('/2'):
            raise UpstreamHTTPError(403, url, response_text='Forbidden')
        return ARTICLE, url
    monkeypatch.setattr('genchi_fetchers.aggregators.BrowserClient.render', render)
    context = FetchContext(emit_record=records.append, emit_asset=lambda *_: None,
                           load_checkpoint=lambda: state.copy(), save_checkpoint=lambda s: state.update(s),
                           secret_provider=lambda _: 'test', logger=None)
    request = FetchRequest(task_id=1, source_id='test', operation='fetch',
                           config=config(min_page_interval_seconds=15, stop_on_access_denied=True).model_dump(), tags=())
    report = AggregatorFetcher().fetch(context, request)
    assert report.status == 'partial' and report.details['page_errors'][0]['http_status'] == 403
    assert moments == [0, 15, 30] and len(records) == 1
    assert state['pending'] == ['https://publisher.test/article/2', 'https://publisher.test/article/3']
    assert not any(u.endswith('/3') for u in requested)
