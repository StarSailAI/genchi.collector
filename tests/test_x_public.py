from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import service
from aiohttp.test_utils import TestClient, TestServer
from allfeeds_sdk import AuthenticationError, FetchRequest, TransientError
from genchi_fetchers import XProfileFetcher
from test_browser_service import FakeBrowser, FakeContext, FakePage
from test_genchi_fetchers import context
from x_parser import failure_code, parse_detail, parse_profile


def article(post_id="1234567890", *, body="ライブ開催\n先行は明日", pinned=False, extra=""):
    return f'''<article>{'<svg data-icon="icon-pin"/>' if pinned else ''}
    <a href="/example">公式</a><a href="https://x.com/example">@example</a>
    <a href="/example/status/{post_id}">2h</a>
    <div dir="auto">{body}</div>{extra}</article>'''


def detail(post_id="1234567890", **kwargs):
    return f'''<meta property="og:url" content="https://x.com/example/status/{post_id}">
    <meta property="article:published_time" content="2026-09-09T04:30:00Z">
    <meta property="og:description" content="This SEO description is truncated…">
    {article(post_id, **kwargs)}'''


def test_public_x_plain_articles_quotes_and_absolute_metadata():
    quote = article("9876543210", body="引用された別の告知")
    html = detail(body='先行受付<br>明日まで <a href="https://official.example/ticket">申込</a>', extra=quote)
    p = parse_detail(html, "https://x.com/example/status/1234567890")
    assert p["text"] == "先行受付\n明日まで 申込"
    assert p["publishedAt"] == "2026-09-09T04:30:00+00:00"
    assert p["links"] == ["https://official.example/ticket"]
    assert p["authorName"] == "公式" and p["textComplete"]
    assert p["quotedPostUrls"] == ["https://x.com/example/status/9876543210"]
    assert parse_profile(html)[0] == [{"id": "1234567890", "authorHandle": "example", "url": p["url"], "pinned": False}]


@pytest.mark.parametrize("change,error", [
    (lambda s: s.replace('content="https://x.com/example/status/1234567890"', 'content="https://x.com/example/status/9999999999"'), "canonical"),
    (lambda s: s.replace("2026-09-09T04:30:00Z", "2h"), "absolute"),
    (lambda s: s.replace("2026-09-09T04:30:00Z", "2026-09-09T04:30:00"), "timezone"),
    (lambda s: s.replace("先行は明日", '先行は明日<button>さらに表示</button>'), "collapsed"),
    (lambda s: s.replace("<article>", "<aside>").replace("</article>", "</aside>"), "article"),
])
def test_public_x_rejects_partial_or_mismatched_details(change, error):
    with pytest.raises(ValueError, match=error):
        parse_detail(change(detail()), "https://x.com/example/status/1234567890")


def test_public_x_legacy_datetime_and_media_only_are_preserved():
    html = detail(body="", extra='<a href="/example/status/1234567890/photo/1">画像</a>')
    p = parse_detail(html, "https://x.com/example/status/1234567890")
    assert p["text"] == "" and len(p["media"]) == 1
    html = detail().replace('property="article:published_time"', 'property="unused"').replace(
        '2h</a>', '2h</a><time datetime="2026-09-09T13:30:00+09:00"/>').replace('div dir="auto"', 'div data-testid="tweetText"')
    assert parse_detail(html, p["url"])["publicationTimeSource"] == "page_datetime"


def test_public_x_login_challenge_and_unknown_empty_are_distinct():
    assert failure_code("Xにログイン", "https://x.com/example") == "LOGIN_REQUIRED"
    assert failure_code("Verify you are human", "https://x.com/example") == "CHALLENGE"
    assert failure_code("Loading...", "https://x.com/example") == "EMPTY_OR_MARKUP_CHANGED"


def test_public_x_anonymous_scroll_ignores_known_pin_and_reads_full_details(monkeypatch):
    class XPage(FakePage):
        def __init__(self, ctx):
            super().__init__(ctx)
            self.scrolled = False
            self.mouse = SimpleNamespace(wheel=self.scroll)

        async def scroll(self, *_):
            self.scrolled = True

        async def content(self):
            if "/status/" in self.url:
                return detail(self.url.rsplit("/", 1)[1])
            return article(pinned=True) + (article("1234567891") if self.scrolled else "")

    async def run():
        monkeypatch.setenv("BROWSER_API_TOKEN", "test-browser-token")
        monkeypatch.setenv("BROWSER_X_AUTH_MODE", "anonymous")
        monkeypatch.setattr(FakeContext, "new_page", lambda ctx: _new(ctx))
        app = service.create_app()
        app.on_startup.clear()
        app.on_cleanup.clear()
        app["browser"] = FakeBrowser()
        app["x_context"] = FakeContext()
        app["x_context"].new_page = AsyncMock(side_effect=AssertionError("must not use account context"))
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/extract/x-profile", headers={"Authorization": "Bearer test-browser-token"},
                                         json={"handle": "example", "knownPostIds": ["1234567890"]})
            payload = await response.json()
            assert response.status == 200, payload
            assert {p["id"] for p in payload["posts"]} == {"1234567890", "1234567891"}
            assert payload["coverage"]["gapPossible"]
            assert payload["coverage"]["previousNonPinnedOverlap"] == 0
            assert payload["errors"] == []
        assert app["browser"].contexts[0].closed and not app["x_context"].closed

    async def _new(ctx):
        return XPage(ctx)

    asyncio.run(run())


def test_public_x_partial_details_dont_advance_checkpoint_or_hash_metrics(monkeypatch):
    p = parse_detail(detail(), "https://x.com/example/status/1234567890")
    response = {"posts": [p], "extractorVersion": "2.0.0", "errors": [{"id": "1234567891", "code": "LOGIN_REQUIRED"}],
                "coverage": {"mode": "anonymous", "gapPossible": False}}
    monkeypatch.setattr("genchi_fetchers.fetchers.requests.post", lambda *a, **kw: SimpleNamespace(
        status_code=200, json=lambda: response))
    records = []
    ctx = context(records, {"known_post_ids": ["1111111111"]})
    req = FetchRequest(task_id=1, source_id="example-x", operation="fetch", tags=(), config={"handle": "example"})
    report = XProfileFetcher().fetch(ctx, req)
    assert report.status == "partial" and ctx.checkpoint() == {"known_post_ids": ["1111111111"]}
    assert "metrics" not in records[0].attributes and "pinned" not in records[0].attributes
    response["posts"][0]["url"] = "https://x.com/example/status/9999999999"
    with pytest.raises(TransientError, match="mismatched"):
        XProfileFetcher().fetch(ctx, req)
    assert len(records) == 1


def test_public_x_internal_auth_failure_is_not_retried_as_page_failure(monkeypatch):
    monkeypatch.setattr("genchi_fetchers.fetchers.requests.post", lambda *a, **kw: SimpleNamespace(status_code=401))
    with pytest.raises(AuthenticationError):
        XProfileFetcher().fetch(context([]), FetchRequest(task_id=1, source_id="x", operation="fetch", tags=(), config={"handle": "example"}))



def test_public_x_navigation_retries_network_reset_but_not_http_login():
    async def run():
        page = SimpleNamespace(
            goto=AsyncMock(side_effect=[RuntimeError("Page.goto: NS_ERROR_NET_RESET"), SimpleNamespace(status=200)]),
            wait_for_selector=AsyncMock(), wait_for_timeout=AsyncMock(),
            content=AsyncMock(return_value=detail()),
        )
        counts = {"pageRetries": 0}
        assert await service._read_x_page(page, "https://x.com/example", counts) == detail()
        assert counts == {"pageRetries": 1} and page.goto.await_count == 2
        page.goto = AsyncMock(return_value=SimpleNamespace(status=403))
        with pytest.raises(ValueError, match="UPSTREAM_HTTP_403"):
            await service._read_x_page(page, "https://x.com/example", counts)
        assert page.goto.await_count == 1
    asyncio.run(run())
