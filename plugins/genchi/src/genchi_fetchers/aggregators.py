"""Bounded, revisitable collection of approved editorial/community event sources."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from itertools import zip_longest
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from allfeeds_builtin.http import SafeHttpClient
from allfeeds_contracts import FetchReport, ResourceRecord
from allfeeds_sdk import (
    ConfigurationError,
    FetchContext,
    FetcherManifest,
    FetcherPlugin,
    FetchRequest,
    PermanentError,
    TransientError,
    UpstreamHTTPError,
)
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .fetchers import BrowserClient, _meta


def clean_url(value: str) -> str | None:
    """Retain native query IDs; remove fragments and known marketing parameters only."""
    try:
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            return None
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        return urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", urlencode(query), ""))
    except ValueError:
        return None


def source_date(value: str | None) -> tuple[datetime | None, str]:
    if not value:
        return None, "TBD"
    match = re.search(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", value)
    if not match:
        return None, "TBD"
    try:
        if match[4]:
            # Respect explicit offsets, otherwise the publisher's local Japanese time.
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                parsed = datetime(*map(int, match.groups()[:5]), int(match[6] or 0))
            return parsed.replace(tzinfo=parsed.tzinfo or ZoneInfo("Asia/Tokyo")), "TIME"
        return datetime(*map(int, match.groups()[:3]), tzinfo=ZoneInfo("Asia/Tokyo")), "DATE"
    except ValueError:
        return None, "TBD"


class AggregatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start_urls: tuple[str, ...]
    detail_pattern: str
    list_selector: str = "a[href]"
    priority_selector: str | None = None
    next_selector: str | None = None
    title_selector: str = "h1"
    content_selector: str
    remove_selector: str = "script,style,nav,aside,form,iframe,.related-posts,.share,.social"
    published_selector: str | None = None
    updated_selector: str | None = None
    source_role: Literal["editorial", "community", "official_operator"] = "editorial"
    page_format: Literal["html", "animate_nuxt"] = "html"
    pages_per_root: int = Field(default=1, ge=1, le=5)
    max_detail_pages: int = Field(default=24, ge=1, le=100)
    refresh_details_per_run: int = Field(default=6, ge=0, le=50)
    max_tracked_details: int = Field(default=1000, ge=10, le=5000)
    browser_url: str = "http://browser:3003"
    browser_token_secret: str = "BROWSER_API_TOKEN"

    @model_validator(mode="after")
    def validate_scope(self):
        if not self.start_urls or len(self.start_urls) > 12 or any(not clean_url(u) for u in self.start_urls):
            raise ValueError("One to twelve public HTTP(S) discovery URLs are required")
        if self.refresh_details_per_run >= self.max_detail_pages:
            raise ValueError("Reserve capacity for newly discovered articles")
        re.compile(self.detail_pattern)
        return self


def parse_article(html: str, url: str, config: AggregatorConfig) -> dict:
    if config.page_format == "animate_nuxt":
        return parse_animate(html, url)
    soup = BeautifulSoup(html, "lxml")
    heading = soup.select_one(config.title_selector)
    roots = soup.select(config.content_selector)
    if not heading or not roots:
        raise ConfigurationError("Aggregator title/body selector did not match")
    title = heading.get_text(" ", strip=True)
    metadata = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        objects = value if isinstance(value, list) else [value]
        for obj in objects:
            if isinstance(obj, dict) and obj.get("@type") in ("NewsArticle", "Article", "BlogPosting"):
                metadata.update(obj)
    dates = {}
    for field, selector, meta_name in [
        ("published", config.published_selector, "article:published_time"),
        ("updated", config.updated_selector, "article:modified_time"),
    ]:
        node = soup.select_one(selector) if selector else None
        raw = (node.get("datetime") or node.get("content") or node.get_text(" ", strip=True)) if node else _meta(soup, meta_name)
        raw = raw or metadata.get("datePublished" if field == "published" else "dateModified")
        dates[field], dates[field + "_precision"] = source_date(raw)
    links, paragraphs = {}, []
    # Parse a copy: removing recommendation widgets must not affect date metadata.
    for original in roots:
        if any(parent in roots for parent in original.parents):
            continue
        root = BeautifulSoup(str(original), "lxml")
        for node in root.select(config.remove_selector):
            node.decompose()
        for anchor in root.select("a[href]"):
            href = str(anchor.get("href") or "")
            target = clean_url(urljoin(url, href))
            # Anime Hack explicitly includes the destination in its /jump/?u=…
            # link. Decode that value without navigating an unapproved host.
            if target and urlsplit(target).hostname == "anime.eiga.com" and urlsplit(target).path == "/jump/":
                target = clean_url(dict(parse_qsl(urlsplit(target).query)).get("u", "")) or target
            if href.startswith("#") or not target or target == clean_url(url):
                continue
            label = anchor.get_text(" ", strip=True)
            links.setdefault(target, label[:300])
            # URLs are part of the document supplied to the model. It may select,
            # but must never invent, an official or ticket URL.
            if label:
                anchor.append(" [" + target + "]")
        paragraphs.append(root.get_text("\n", strip=True))
    body = "\n".join(paragraphs)
    if len(body) < 40 or not title:
        raise ConfigurationError("Aggregator article is empty or implausibly short")
    if len(body) > 120000 or len(links) > 200:
        raise ConfigurationError("Aggregator body exceeded extraction bound; check selectors")
    return {
        "title": title, "content": title + "\n" + body, **dates,
        "links": [{"url": u, "label": label} for u, label in links.items()],
        "media": [{"type": "image", "url": image}] if (image := _meta(soup, "og:image")) else [],
        **({"original_publisher": {"label": (publisher.get("class") or [publisher.get_text(" ", strip=True)])[0],
                                   "url": clean_url(publisher.get("href") or "")}}
           if urlsplit(url).hostname == "www.livefans.jp" and (publisher := soup.select_one("h4.exLink a[href]")) else {}),
    }


def parse_animate(html: str, url: str) -> dict:
    """Read public serialized page data without executing publisher JavaScript."""
    soup = BeautifulSoup(html, "lxml")
    script = soup.select_one("#__NUXT_DATA__")
    if not script or len(script.get_text()) > 2000000:
        raise ConfigurationError("Animate public event data missing or too large")
    try:
        data = json.loads(script.get_text())
        if not isinstance(data, list) or len(data) > 20000:
            raise ValueError("Invalid reference table")

        def read(index, trail=()):
            if not isinstance(index, int) or index < 0 or index >= len(data) or index in trail or len(trail) > 25:
                raise ValueError("Invalid/cyclic public data reference")
            value = data[index]
            if isinstance(value, dict):
                return {k: read(v, (*trail, index)) for k, v in value.items()}
            if isinstance(value, list):
                return [read(v, (*trail, index)) for v in value]
            return value

        found = [read(i) for i, obj in enumerate(data) if isinstance(obj, dict) and {"slug", "eventShops", "eventContents"} <= obj.keys()]
        found = [obj for obj in found if obj.get("slug") == urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]]
        if len(found) != 1:
            raise ValueError("Event identity did not uniquely match the URL")
        event = found[0]
    except (ValueError, TypeError, IndexError, RecursionError) as exc:
        raise ConfigurationError("Animate public event data could not be decoded") from exc
    title = str(event.get("name") or "")
    lines = [title, BeautifulSoup(event.get("description") or "", "lxml").get_text("\n", strip=True)]
    media = []
    for asset in [event.get("image") or {}, *[c.get("image") or {} for c in event.get("eventContents") or []]]:
        image = clean_url(asset.get("url") or "")
        if image:
            media.append({"type": "image", "url": image})
    for occurrence in event.get("eventShops") or []:
        shop = occurrence.get("shop") or {}
        lines.extend(["開催店舗: " + str(shop.get("name") or ""), "住所: " + str(shop.get("address") or "")])
        for key, label in [("startsAt", "開催開始日"), ("endsAt", "開催終了日")]:
            value, _ = source_date(occurrence.get(key))
            if value:
                # CMS midnight/23:59:59 bounds express dates, not actual business hours.
                lines.append(label + ": " + value.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat())
        if shop.get("openingHours"):
            lines.append("店舗の営業時間・入場案内（開催期間と分けて確認）: " + shop["openingHours"])
    for item in event.get("eventContents") or []:
        note = BeautifulSoup(item.get("note") or "", "lxml").get_text("\n", strip=True)
        if note:
            lines.append(str(item.get("type") or "案内") + ": " + note)
    if event.get("eventStartsAt"):
        for key, label in [("eventStartsAt", "イベント開始日"), ("eventEndsAt", "イベント終了日")]:
            value, _ = source_date(event.get(key))
            if value:
                lines.append(label + ": " + value.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat())
    if not title or len("\n".join(lines)) < 40:
        raise ConfigurationError("Animate event has no usable public facts")
    updated, precision = source_date(event.get("updatedAt"))
    return {"title": title, "content": "\n".join(lines), "published": None, "published_precision": "TBD",
            "updated": updated, "updated_precision": precision, "links": [], "media": media,
            "image_details_pending": bool(media)}


class AggregatorFetcher(FetcherPlugin):
    manifest = FetcherManifest(name="genchi.aggregator", version="0.1.0", operations=("fetch",), default_queue="browser", default_timeout_seconds=3600, capabilities=("browser_api",))
    config_model = AggregatorConfig

    def fetch(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = AggregatorConfig.model_validate(request.config)
        hosts = {urlsplit(u).hostname for u in config.start_urls}
        pattern = re.compile(config.detail_pattern)
        browser = BrowserClient(config.browser_url, context.secret(config.browser_token_secret))
        http = SafeHttpClient(user_agent="GenchiCollector/0.1 (+https://genchi.news)", timeout_seconds=30,
                              retries=1, max_response_bytes=1000000, obey_robots=True,
                              allow_private_network=False, allowed_hosts=(), rate_limit_seconds=2)

        def load(url, selector):
            if urlsplit(url).hostname not in hosts:
                raise PermanentError("Aggregator navigation left its configured host scope")
            http.validate_url(url)
            if not http._allowed_by_robots(url):
                raise PermanentError("Publisher robots policy excludes this aggregator URL")
            html, final = browser.render(url, selector=selector)
            if urlsplit(final).hostname not in hosts:
                raise PermanentError("Aggregator redirected outside configured host scope")
            return html, final

        state = context.checkpoint()
        tracked = [u for u in state.get("tracked", []) if urlsplit(u).hostname in hosts and pattern.fullmatch(u)]
        roots, list_pages, errors = [], 0, []
        for root_url in config.start_urls:
            root_links, visited = [], set()
            page = root_url
            for _ in range(config.pages_per_root):
                if page in visited:
                    break
                try:
                    html, final = load(page, config.list_selector)
                except (TransientError, UpstreamHTTPError) as exc:
                    errors.append({"url": page, "stage": "discovery", "error": type(exc).__name__})
                    break
                visited.add(page)
                list_pages += 1
                soup = BeautifulSoup(html, "lxml")
                priority = soup.select(config.priority_selector) if config.priority_selector else []
                for a in [*priority, *soup.select(config.list_selector)]:
                    url = clean_url(urljoin(final, str(a.get("href") or "")))
                    if url and urlsplit(url).hostname in hosts and pattern.fullmatch(url) and url not in root_links:
                        root_links.append(url)
                nxt = soup.select_one(config.next_selector) if config.next_selector else None
                if not nxt or not nxt.get("href"):
                    break
                page = clean_url(urljoin(final, str(nxt["href"])))
                if not page:
                    break
            roots.append(root_links)
        discovered = list(dict.fromkeys(u for group in zip_longest(*roots) for u in group if u))
        if not discovered:
            raise TransientError("Aggregator discovery yielded no articles; selectors or access need verification")
        # New articles are queued persistently before older refreshes, so a busy
        # first page cannot starve the other categories or previously discovered tail.
        previous_pending = state.get("pending", [])
        fresh = [u for u in discovered if u not in tracked]
        # Reserve part of discovery for older pending pages, while new notices
        # do not wait behind an entire first-run calendar or historical backlog.
        backlog = list(dict.fromkeys([*fresh, *previous_pending]))
        backlog = [u for u in backlog if urlsplit(u).hostname in hosts and pattern.fullmatch(u)]
        refresh_count = min(config.refresh_details_per_run, len(tracked))
        cursor = int(state.get("refresh_cursor", 0))
        refresh = [tracked[(cursor + i) % len(tracked)] for i in range(refresh_count)]
        slots = config.max_detail_pages - refresh_count
        carried = [u for u in previous_pending if u in backlog]
        carry_slots = min(len(carried), max(1, slots // 3)) if fresh else slots
        selected = list(dict.fromkeys([*carried[:carry_slots], *fresh]))[:slots]
        selected += [u for u in backlog if u not in selected][:slots - len(selected)]
        selected += [u for u in refresh if u not in selected]
        emitted, missing = [], []
        for url in selected:
            try:
                html, final = load(url, config.title_selector if config.page_format == "html" else "#__NUXT_DATA__")
            except UpstreamHTTPError as exc:
                if exc.status_code not in {404, 410}:
                    errors.append({"url": url, "stage": "article", "error": type(exc).__name__, "http_status": exc.status_code})
                    continue
                missing.append(url)
                continue
            except TransientError as exc:
                errors.append({"url": url, "stage": "article", "error": type(exc).__name__})
                continue
            canonical = clean_url(final)
            if not canonical or not pattern.fullmatch(canonical):
                raise ConfigurationError("Detail redirected to a non-article page")
            try:
                article = parse_article(html, canonical, config)
            except ConfigurationError as exc:
                errors.append({"url": url, "stage": "parsing", "error": str(exc)})
                continue
            context.emit(ResourceRecord(
                external_id="article:" + hashlib.sha256(canonical.encode()).hexdigest(),
                kind="aggregate_article", url=canonical, title=article["title"], content=article["content"],
                content_type="text/plain", language="ja", published_at=article["published"], observed_at=datetime.now(UTC),
                attributes={"source_type": "aggregator", "source_role": config.source_role,
                            "published_precision": article["published_precision"],
                            "updated_precision": article["updated_precision"],
                            "upstream_updated_at": article["updated"].isoformat() if article["updated"] else None,
                            "outbound_links": article["links"], "media": article["media"],
                            "image_details_pending": article.get("image_details_pending", False),
                            **({"original_publisher": article["original_publisher"]} if article.get("original_publisher") else {})}, tags=request.tags,
            ))
            emitted.append(url)
        done = set(emitted + missing)
        next_tracked = [u for u in dict.fromkeys([*tracked, *emitted]) if u not in missing]
        pending = list(dict.fromkeys([u for u in selected if u not in done] + [u for u in backlog if u not in done]))
        if errors and not emitted:
            raise TransientError("No aggregator article could be saved; failures: " + str(errors[:3]))
        overflow = len(next_tracked) > config.max_tracked_details or len(pending) > config.max_tracked_details
        # Never silently discard coverage to make a bounded run look complete.
        if overflow:
            raise ConfigurationError("Aggregator tracking capacity exceeded; narrow scope or increase its bound")
        checkpoint = {"tracked": next_tracked, "pending": pending,
                      "refresh_cursor": (cursor + refresh_count) % max(1, len(next_tracked))}
        context.set_checkpoint(checkpoint)
        return FetchReport(status="partial" if errors else "succeeded",
                           error_message=f"{len(errors)} upstream pages require another attempt" if errors else None,
                           details={"list_pages": list_pages, "discovered": len(discovered), "articles": len(emitted),
                                    "pending_articles": len(pending), "refreshed": len([u for u in emitted if u in tracked]),
                                    "missing_details": missing, "page_errors": errors, "source_role": config.source_role})
