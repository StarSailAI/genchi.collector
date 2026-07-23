from __future__ import annotations

import json
from typing import Any

import pytest
from allfeeds_builtin import JsonApiFetcher, RssFetcher, WebListFetcher
from allfeeds_builtin.http import SafeHttpClient
from allfeeds_contracts import ResourceAsset, ResourceRecord
from allfeeds_sdk import FetchContext, FetchRequest, PermanentError


class Response:
    def __init__(
        self,
        content: bytes,
        *,
        url: str,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ):
        self.content = content
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> Any:
        return json.loads(self.content)


class FakeClient:
    def __init__(self, responses: dict[str, Response]):
        self.responses = responses

    def get(self, url: str, **_kwargs) -> Response:
        return self.responses[url]


def context(records: list[ResourceRecord], assets: list[ResourceAsset], state: dict[str, Any]):
    return FetchContext(
        emit_record=records.append,
        emit_asset=lambda asset, _content: assets.append(asset),
        load_checkpoint=lambda: dict(state),
        save_checkpoint=lambda value: (state.clear(), state.update(value)),
        secret_provider=lambda _name: None,
        logger=None,
    )


def request(config: dict[str, Any]) -> FetchRequest:
    return FetchRequest(
        task_id=1, source_id="test", operation="fetch", config=config, tags=("test",)
    )


def test_rss_fetcher_emits_content_and_checkpoint(monkeypatch) -> None:
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
    <item><guid>item-1</guid><title>Hello</title><link>https://example.com/1</link>
    <description>Real body text</description><pubDate>Sat, 18 Jul 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""
    fake = FakeClient(
        {
            "https://example.com/feed.xml": Response(
                xml, url="https://example.com/feed.xml", headers={"ETag": '"v1"'}
            )
        }
    )
    monkeypatch.setattr("allfeeds_builtin.fetchers._client", lambda *_args: fake)
    records, assets, state = [], [], {}
    report = RssFetcher().fetch(
        context(records, assets, state),
        request({"url": "https://example.com/feed.xml"}),
    )
    assert report.status == "succeeded"
    assert records[0].external_id == "item-1"
    assert "Real body text" in (records[0].content or "")
    assert state["etag"] == '"v1"'


def test_web_list_fetches_details(monkeypatch) -> None:
    fake = FakeClient(
        {
            "https://example.com/list": Response(
                b'<html><a class="item" href="/one">One</a></html>',
                url="https://example.com/list",
            ),
            "https://example.com/one": Response(
                b"<html><h1>One</h1><article>Full text</article></html>",
                url="https://example.com/one",
            ),
        }
    )
    monkeypatch.setattr("allfeeds_builtin.fetchers._client", lambda *_args: fake)
    records, assets, state = [], [], {}
    WebListFetcher().fetch(
        context(records, assets, state),
        request(
            {
                "start_urls": ["https://example.com/list"],
                "item_selector": "a.item",
                "detail_link_selector": "a.item",
                "detail": {"title_selector": "h1", "content_selector": "article"},
            }
        ),
    )
    assert len(records) == 1
    assert records[0].title == "One"
    assert records[0].content == "Full text"


def test_json_api_maps_jmespath(monkeypatch) -> None:
    fake = FakeClient(
        {
            "https://api.example.com/items": Response(
                json.dumps({"data": [{"id": 7, "name": "Seven", "body": "Content"}]}).encode(),
                url="https://api.example.com/items",
            )
        }
    )
    monkeypatch.setattr("allfeeds_builtin.fetchers._client", lambda *_args: fake)
    records, assets, state = [], [], {}
    JsonApiFetcher().fetch(
        context(records, assets, state),
        request(
            {
                "url": "https://api.example.com/items",
                "items_path": "data",
                "fields": {"external_id": "id", "title": "name", "content": "body"},
            }
        ),
    )
    assert records[0].external_id == "7"
    assert records[0].title == "Seven"


def test_http_client_blocks_loopback() -> None:
    client = SafeHttpClient(
        user_agent="test",
        timeout_seconds=1,
        retries=0,
        max_response_bytes=1024,
        obey_robots=False,
        allow_private_network=False,
        allowed_hosts=(),
        rate_limit_seconds=0,
    )
    with pytest.raises(PermanentError):
        client.validate_url("http://127.0.0.1/admin")
