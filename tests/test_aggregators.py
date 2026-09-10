from __future__ import annotations

import json

import pytest
from allfeeds_sdk import FetchContext, FetchRequest, PermanentError, TransientError
from genchi_fetchers.aggregators import (
    AggregatorConfig,
    AggregatorFetcher,
    clean_url,
    parse_article,
    source_date,
)
from genchi_product.pipeline import _text_candidates


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

    def render(_self, url):
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
    with pytest.raises(TransientError):
        fetcher.fetch(context, request)
    assert state == previous
    broken.clear()
    fetcher.fetch(context, request)
    assert state["pending"] == [] and len(state["tracked"]) == 5
    assert records[3].external_id == records[4].external_id  # replay of partially written article 4
    monkeypatch.setattr("genchi_fetchers.aggregators.SafeHttpClient._allowed_by_robots", lambda *_: False)
    with pytest.raises(PermanentError):
        fetcher.fetch(context, request)


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
