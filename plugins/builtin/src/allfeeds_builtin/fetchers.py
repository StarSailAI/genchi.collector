from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import feedparser
import jmespath
import soupsieve
from allfeeds_contracts import FetchReport, ResourceAsset, ResourceRecord
from allfeeds_sdk import (
    ConfigurationError,
    FetchContext,
    FetcherManifest,
    FetcherPlugin,
    FetchRequest,
)
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .http import SafeHttpClient

UTC = UTC


def _stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part or "") for part in parts).encode()).hexdigest()


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    for parser in (
        lambda raw: datetime.fromisoformat(str(raw).replace("Z", "+00:00")),
        lambda raw: parsedate_to_datetime(str(raw)),
    ):
        try:
            result = parser(value)
            return result if result.tzinfo else result.replace(tzinfo=UTC)
        except (TypeError, ValueError, IndexError):
            continue
    return None


class HttpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    headers: dict[str, str] = Field(default_factory=dict)
    bearer_token_secret: str | None = None
    user_agent: str = "AllFeeds/0.1"
    timeout_seconds: float = Field(default=30, ge=1, le=300)
    retries: int = Field(default=3, ge=0, le=10)
    max_response_bytes: int = Field(default=10_000_000, ge=1024, le=500_000_000)
    obey_robots: bool = True
    allow_private_network: bool = False
    allowed_hosts: tuple[str, ...] = ()
    rate_limit_seconds: float = Field(default=1, ge=0, le=300)


def _client(config: HttpConfig, context: FetchContext) -> SafeHttpClient:
    headers = dict(config.headers)
    if config.bearer_token_secret:
        headers["Authorization"] = f"Bearer {context.secret(config.bearer_token_secret)}"
    return SafeHttpClient(
        user_agent=config.user_agent,
        timeout_seconds=config.timeout_seconds,
        retries=config.retries,
        max_response_bytes=config.max_response_bytes,
        obey_robots=config.obey_robots,
        allow_private_network=config.allow_private_network,
        allowed_hosts=config.allowed_hosts,
        rate_limit_seconds=config.rate_limit_seconds,
        headers=headers,
    )


def _extract(element: Any, selector: str | None, *, attribute: str | None = None) -> str | None:
    if not selector:
        return None
    selected = element.select_one(selector)
    if not selected:
        return None
    if attribute:
        value = selected.get(attribute)
        return str(value).strip() if value is not None else None
    return selected.get_text("\n", strip=True) or None


class RssConfig(HttpConfig):
    url: str
    max_items: int = Field(default=200, ge=1, le=5000)
    fulltext: bool = False
    fulltext_selectors: tuple[str, ...] = ("article", "main")
    download_enclosures: bool = False
    kind: str = "article"


class RssFetcher(FetcherPlugin):
    manifest = FetcherManifest(
        name="builtin.rss",
        version="0.1.0",
        operations=("fetch", "backfill"),
        default_queue="web",
        default_timeout_seconds=300,
    )
    config_model = RssConfig

    def fetch(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = RssConfig.model_validate(request.config)
        client = _client(config, context)
        state = context.checkpoint()
        headers = {}
        if state.get("etag"):
            headers["If-None-Match"] = str(state["etag"])
        if state.get("last_modified"):
            headers["If-Modified-Since"] = str(state["last_modified"])
        response = client.get(config.url, headers=headers)
        if response.status_code == 304:
            return FetchReport(details={"upstream_status": "not_modified"})
        parsed = feedparser.parse(response.content)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            raise ConfigurationError(
                f"RSS parse failed: {getattr(parsed, 'bozo_exception', 'unknown')}"
            )
        for item in parsed.entries[: config.max_items]:
            url = str(item.get("link") or "") or None
            external_id = str(item.get("id") or item.get("guid") or url or item.get("title") or "")
            if not external_id:
                external_id = _stable_id(json.dumps(dict(item), default=str, sort_keys=True))
            title = str(item.get("title") or "").strip() or None
            content = ""
            values = item.get("content") or []
            if values:
                content = str(values[0].get("value") or "")
            content = content or str(item.get("summary") or item.get("description") or "")
            if config.fulltext and url:
                page = client.get(url, allowed_content_types=("text/html", "application/xhtml+xml"))
                soup = BeautifulSoup(page.content, "lxml")
                for selector in config.fulltext_selectors:
                    selected = soup.select_one(selector)
                    if selected:
                        full = selected.get_text("\n", strip=True)
                        if len(full) > len(content):
                            content = full
                        break
            context.emit(
                ResourceRecord(
                    external_id=external_id,
                    kind=config.kind,
                    url=url,
                    title=title,
                    content=content or title,
                    content_type="text/markdown",
                    published_at=_time(item.get("published") or item.get("updated")),
                    observed_at=datetime.now(UTC),
                    attributes={
                        "author": item.get("author"),
                        "feed_title": parsed.feed.get("title"),
                    },
                    tags=request.tags,
                )
            )
            for enclosure in item.get("enclosures") or ():
                enclosure_url = str(enclosure.get("href") or enclosure.get("url") or "")
                if not enclosure_url:
                    continue
                content_bytes = None
                if config.download_enclosures:
                    content_bytes = client.get(enclosure_url).content
                context.emit_asset(
                    ResourceAsset(
                        external_id=external_id,
                        asset_key=_stable_id(enclosure_url)[:24],
                        url=enclosure_url,
                        media_type=enclosure.get("type"),
                    ),
                    content_bytes,
                )
        context.set_checkpoint(
            {
                "etag": response.headers.get("ETag") or state.get("etag"),
                "last_modified": response.headers.get("Last-Modified")
                or state.get("last_modified"),
                "last_success_at": datetime.now(UTC).isoformat(),
            }
        )
        return FetchReport(details={"upstream_status": "ok" if parsed.entries else "empty"})


class WebPageConfig(HttpConfig):
    url: str = ""
    kind: str = "web_page"
    title_selector: str | None = "title"
    content_selector: str = "body"
    published_selector: str | None = None
    external_id_selector: str | None = None
    external_id_attribute: str | None = None
    attribute_selectors: dict[str, str] = Field(default_factory=dict)
    asset_selector: str | None = None
    asset_attribute: str = "href"


def _page_record(
    soup: BeautifulSoup,
    *,
    url: str,
    config: WebPageConfig,
    tags: tuple[str, ...],
) -> ResourceRecord:
    title = _extract(soup, config.title_selector)
    content = _extract(soup, config.content_selector)
    external_id = (
        _extract(soup, config.external_id_selector, attribute=config.external_id_attribute) or url
    )
    attributes = {
        key: _extract(soup, selector) for key, selector in config.attribute_selectors.items()
    }
    return ResourceRecord(
        external_id=external_id,
        kind=config.kind,
        url=url,
        title=title,
        content=content,
        content_type="text/plain",
        published_at=_time(_extract(soup, config.published_selector)),
        observed_at=datetime.now(UTC),
        attributes={key: value for key, value in attributes.items() if value is not None},
        tags=tags,
    )


class WebPageFetcher(FetcherPlugin):
    manifest = FetcherManifest(
        name="builtin.web_page",
        version="0.1.0",
        operations=("fetch", "backfill"),
        default_queue="web",
        default_timeout_seconds=300,
    )
    config_model = WebPageConfig

    def fetch(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = WebPageConfig.model_validate(request.config)
        if not config.url:
            raise ConfigurationError("url is required")
        client = _client(config, context)
        response = client.get(
            config.url, allowed_content_types=("text/html", "application/xhtml+xml")
        )
        soup = BeautifulSoup(response.content, "lxml")
        record = _page_record(soup, url=response.url, config=config, tags=request.tags)
        context.emit(record)
        if config.asset_selector:
            for index, item in enumerate(soup.select(config.asset_selector)):
                raw = item.get(config.asset_attribute)
                if raw:
                    url = urljoin(response.url, str(raw))
                    context.emit_asset(
                        ResourceAsset(
                            external_id=record.external_id,
                            asset_key=f"asset-{index}-{_stable_id(url)[:12]}",
                            url=url,
                        )
                    )
        return FetchReport(details={"upstream_status": "ok"})


class WebListConfig(HttpConfig):
    start_urls: tuple[str, ...]
    item_selector: str
    detail_link_selector: str
    detail_link_attribute: str = "href"
    next_selector: str | None = None
    next_attribute: str = "href"
    max_pages: int = Field(default=10, ge=1, le=1000)
    max_items: int = Field(default=500, ge=1, le=100_000)
    detail: WebPageConfig


class WebListFetcher(FetcherPlugin):
    manifest = FetcherManifest(
        name="builtin.web_list",
        version="0.1.0",
        operations=("fetch", "backfill"),
        default_queue="web",
        default_timeout_seconds=900,
    )
    config_model = WebListConfig

    def fetch(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = WebListConfig.model_validate(request.config)
        client = _client(config, context)
        seen_urls: set[str] = set()
        remaining = list(config.start_urls)
        pages = 0
        while remaining and pages < config.max_pages and len(seen_urls) < config.max_items:
            list_url = remaining.pop(0)
            response = client.get(
                list_url, allowed_content_types=("text/html", "application/xhtml+xml")
            )
            soup = BeautifulSoup(response.content, "lxml")
            pages += 1
            for item in soup.select(config.item_selector):
                link = item.select_one(config.detail_link_selector)
                if link is None and soupsieve.match(config.detail_link_selector, item):
                    link = item
                raw = link.get(config.detail_link_attribute) if link else None
                if not raw:
                    continue
                detail_url = urljoin(response.url, str(raw))
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                page = client.get(
                    detail_url, allowed_content_types=("text/html", "application/xhtml+xml")
                )
                detail_soup = BeautifulSoup(page.content, "lxml")
                detail_config = config.detail.model_copy(update={"url": detail_url})
                context.emit(
                    _page_record(detail_soup, url=page.url, config=detail_config, tags=request.tags)
                )
                if len(seen_urls) >= config.max_items:
                    break
            if config.next_selector:
                next_link = soup.select_one(config.next_selector)
                raw_next = next_link.get(config.next_attribute) if next_link else None
                next_url = urljoin(response.url, str(raw_next)) if raw_next else None
                if next_url and next_url not in remaining:
                    remaining.append(next_url)
        return FetchReport(details={"pages": pages, "detail_urls": len(seen_urls)})


class JsonApiConfig(HttpConfig):
    url: str
    items_path: str
    fields: dict[str, str]
    external_id_field: str = "external_id"
    page_param: str | None = None
    page_start: int = 1
    max_pages: int = Field(default=1, ge=1, le=10_000)
    next_path: str | None = None
    kind: str = "api_record"

    @field_validator("fields")
    @classmethod
    def require_external_id(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("fields cannot be empty")
        return value


class JsonApiFetcher(FetcherPlugin):
    manifest = FetcherManifest(
        name="builtin.json_api",
        version="0.1.0",
        operations=("fetch", "backfill"),
        default_queue="web",
        default_timeout_seconds=600,
    )
    config_model = JsonApiConfig

    def fetch(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = JsonApiConfig.model_validate(request.config)
        client = _client(config, context)
        url = config.url
        pages = 0
        while url and pages < config.max_pages:
            if config.page_param:
                separator = "&" if "?" in url else "?"
                page_url = f"{url}{separator}{config.page_param}={config.page_start + pages}"
            else:
                page_url = url
            response = client.get(page_url, allowed_content_types=("application/json", "text/json"))
            payload = response.json()
            items = jmespath.search(config.items_path, payload) or []
            if not isinstance(items, list):
                raise ConfigurationError("items_path must resolve to a JSON list")
            for item in items:
                mapped = {key: jmespath.search(path, item) for key, path in config.fields.items()}
                external_id = mapped.get(config.external_id_field)
                if external_id is None:
                    external_id = _stable_id(json.dumps(item, sort_keys=True, default=str))
                known = {
                    "external_id",
                    "url",
                    "title",
                    "content",
                    "content_type",
                    "language",
                    "published_at",
                }
                context.emit(
                    ResourceRecord(
                        external_id=str(external_id),
                        kind=config.kind,
                        url=str(mapped["url"]) if mapped.get("url") is not None else None,
                        title=str(mapped["title"]) if mapped.get("title") is not None else None,
                        content=str(mapped["content"])
                        if mapped.get("content") is not None
                        else None,
                        content_type=str(mapped["content_type"])
                        if mapped.get("content_type")
                        else "application/json",
                        language=str(mapped["language"]) if mapped.get("language") else None,
                        published_at=_time(mapped.get("published_at")),
                        observed_at=datetime.now(UTC),
                        attributes={
                            key: value for key, value in mapped.items() if key not in known
                        },
                        tags=request.tags,
                    )
                )
            pages += 1
            if config.page_param:
                if not items:
                    break
            elif config.next_path:
                next_value = jmespath.search(config.next_path, payload)
                url = urljoin(response.url, str(next_value)) if next_value else ""
            else:
                break
        return FetchReport(details={"pages": pages})


class SitemapConfig(HttpConfig):
    url: str
    max_urls: int = Field(default=1000, ge=1, le=100_000)
    page: WebPageConfig


class SitemapFetcher(FetcherPlugin):
    manifest = FetcherManifest(
        name="builtin.sitemap",
        version="0.1.0",
        operations=("fetch", "backfill"),
        default_queue="web",
        default_timeout_seconds=1800,
    )
    config_model = SitemapConfig

    def fetch(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = SitemapConfig.model_validate(request.config)
        client = _client(config, context)
        pending = [config.url]
        page_urls: list[str] = []
        visited: set[str] = set()
        while pending and len(page_urls) < config.max_urls:
            sitemap_url = pending.pop(0)
            if sitemap_url in visited:
                continue
            visited.add(sitemap_url)
            response = client.get(sitemap_url)
            root = ET.fromstring(response.content)
            locations = [
                node.text.strip() for node in root.iter() if node.tag.endswith("loc") and node.text
            ]
            if root.tag.endswith("sitemapindex"):
                pending.extend(locations)
            else:
                page_urls.extend(locations[: config.max_urls - len(page_urls)])
        for url in page_urls:
            response = client.get(url, allowed_content_types=("text/html", "application/xhtml+xml"))
            soup = BeautifulSoup(response.content, "lxml")
            page_config = config.page.model_copy(update={"url": url})
            context.emit(
                _page_record(soup, url=response.url, config=page_config, tags=request.tags)
            )
        return FetchReport(details={"sitemaps": len(visited), "pages": len(page_urls)})
