from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

import requests
from allfeeds_builtin.http import SafeHttpClient
from allfeeds_contracts import FetchReport, ResourceRecord
from allfeeds_sdk import (
    ConfigurationError,
    FetchContext,
    FetcherManifest,
    FetcherPlugin,
    FetchRequest,
    TransientError,
)
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    for parser in (
        lambda raw: datetime.fromisoformat(str(raw).replace("Z", "+00:00")),
        lambda raw: parsedate_to_datetime(str(raw)),
        lambda raw: datetime.strptime(str(raw).strip(), "%Y.%m.%d").replace(tzinfo=UTC),
        lambda raw: datetime.strptime(str(raw).strip(), "%Y/%m/%d").replace(tzinfo=UTC),
    ):
        try:
            result = parser(value)
            return result if result.tzinfo else result.replace(tzinfo=UTC)
        except (TypeError, ValueError, IndexError):
            continue
    return None


def _text(node: Any, selector: str | None) -> str | None:
    selected = node.select_one(selector) if selector else None
    return selected.get_text("\n", strip=True) if selected else None


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        node = soup.select_one(f'meta[property="{name}"], meta[name="{name}"]')
        if node and node.get("content"):
            return str(node["content"]).strip()
    return None


class BrowserClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def render(self, url: str, *, selector: str | None = None) -> tuple[str, str]:
        response = requests.post(
            f"{self.base_url}/fetch",
            headers=self.headers,
            json={"url": url, "selector": selector, "waitSeconds": 3, "timeoutSeconds": 90},
            timeout=110,
        )
        if response.status_code >= 400:
            raise TransientError(f"browser render failed with HTTP {response.status_code}")
        upstream_status = response.headers.get("X-Genchi-Browser-Upstream-Status")
        if upstream_status and int(upstream_status) >= 500:
            raise TransientError(f"browser upstream returned HTTP {upstream_status}")
        return response.text, response.headers.get("X-Genchi-Browser-Final-URL", url)


class XProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    handle: str = Field(pattern=r"^[A-Za-z0-9_]{1,15}$")
    browser_url: str = "http://browser:3003"
    api_token_secret: str = "BROWSER_API_TOKEN"
    max_posts: int = Field(default=50, ge=1, le=200)
    max_scrolls: int = Field(default=8, ge=0, le=30)


class XProfileFetcher(FetcherPlugin):
    manifest = FetcherManifest(
        name="genchi.x_profile",
        version="0.1.0",
        operations=("fetch",),
        default_queue="browser",
        default_timeout_seconds=180,
        capabilities=("browser_api",),
    )
    config_model = XProfileConfig

    def fetch(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = XProfileConfig.model_validate(request.config)
        token = context.secret(config.api_token_secret)
        checkpoint = context.checkpoint()
        known_ids = [str(value) for value in checkpoint.get("known_post_ids") or []][:200]
        response = requests.post(
            f"{config.browser_url.rstrip('/')}/extract/x-profile",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "handle": config.handle,
                "maxPosts": config.max_posts,
                "maxScrolls": config.max_scrolls,
                "knownPostIds": known_ids,
            },
            timeout=210,
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("code") or response.json().get("detail")
            except ValueError:
                detail = response.text[:200]
            raise TransientError(f"X extraction failed: {detail}")
        payload = response.json()
        posts = payload.get("posts") or []
        if not posts:
            raise TransientError("X extraction returned no posts")
        observed_at = _time(payload.get("observedAt")) or datetime.now(UTC)
        emitted_ids: list[str] = []
        for post in posts:
            post_id = str(post["id"])
            emitted_ids.append(post_id)
            context.emit(
                ResourceRecord(
                    external_id=f"x:{post_id}",
                    kind="x_post",
                    url=post.get("url"),
                    title=(post.get("text") or "")[:160] or None,
                    content=post.get("text"),
                    content_type="text/plain",
                    language="ja",
                    published_at=_time(post.get("publishedAt")),
                    observed_at=observed_at,
                    attributes={
                        "platform": "x",
                        "post_id": post_id,
                        "author_handle": post.get("authorHandle") or config.handle,
                        "author_name": post.get("authorName"),
                        "links": post.get("links") or [],
                        "hashtags": post.get("hashtags") or [],
                        "media": post.get("media") or [],
                        "metrics": post.get("metrics") or {},
                        "pinned": bool(post.get("pinned")),
                        "extractor_version": payload.get("extractorVersion"),
                    },
                    tags=request.tags,
                )
            )
        merged_ids = list(dict.fromkeys([*emitted_ids, *known_ids]))[:200]
        context.set_checkpoint(
            {"known_post_ids": merged_ids, "last_success_at": observed_at.isoformat()}
        )
        return FetchReport(details={"handle": config.handle, "posts": len(posts)})


def _plain_html(value: Any) -> str:
    if not value:
        return ""
    return BeautifulSoup(str(value), "lxml").get_text("\n", strip=True)


def _jsonapi_index(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(item.get("type")), str(item.get("id"))): item
        for item in payload.get("included") or []
        if isinstance(item, dict) and item.get("type") and item.get("id")
    }


def _relationship(
    item: dict[str, Any],
    name: str,
    included: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    reference = ((item.get("relationships") or {}).get(name) or {}).get("data")
    if not isinstance(reference, dict):
        return None
    return included.get((str(reference.get("type")), str(reference.get("id"))))


def _asobi_project(tags: tuple[str, ...], booth: dict[str, Any]) -> tuple[str, ...]:
    name = str((booth.get("attributes") or {}).get("name") or "").lower()
    slug = str((booth.get("attributes") or {}).get("slug") or booth.get("id") or "").lower()
    project = "tales" if "tales" in slug or "テイルズ" in name else "idolmaster"
    return tuple(tag for tag in tags if not tag.startswith("project:")) + (f"project:{project}",)


def _image_media(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    media = []
    for item in items:
        if item.get("type") != "image":
            continue
        urls = (item.get("attributes") or {}).get("reading_urls") or {}
        url = urls.get("large") or urls.get("medium") or urls.get("original")
        if url:
            media.append({"type": "image", "url": str(url)})
    return media


class AsobiTicketConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_base_url: str = "https://asobi-ticket.api.app.t-riple.com/api/v1/public"
    site_base_url: str = "https://asobiticket2.asobistore.jp"
    per_page: int = Field(default=50, ge=1, le=100)
    max_pages: int = Field(default=20, ge=1, le=100)
    max_booths: int = Field(default=500, ge=1, le=5000)
    max_content_chars: int = Field(default=120_000, ge=1000, le=500_000)


class AsobiTicketFetcher(FetcherPlugin):
    manifest = FetcherManifest(
        name="genchi.asobi_ticket",
        version="0.1.0",
        operations=("fetch",),
        default_queue="web",
        default_timeout_seconds=1200,
    )
    config_model = AsobiTicketConfig

    @staticmethod
    def _get(client: SafeHttpClient, url: str) -> dict[str, Any]:
        response = client.get(url, allowed_content_types=("application/json",))
        payload = response.json()
        if not isinstance(payload, dict):
            raise ConfigurationError(f"ASOBI TICKET returned a non-object response for {url}")
        return payload

    @staticmethod
    def _act_content(act: dict[str, Any], tour: dict[str, Any] | None) -> str:
        attributes = act.get("attributes") or {}
        tour_name = ((tour or {}).get("attributes") or {}).get("name")
        values = [
            f"イベント: {tour_name}" if tour_name else None,
            f"公演: {attributes.get('name')}" if attributes.get("name") else None,
            f"会場: {attributes.get('venue')}" if attributes.get("venue") else None,
            f"公演日: {attributes.get('performance_date')}"
            if attributes.get("performance_date")
            else None,
            f"開場: {attributes.get('opens_at')}" if attributes.get("opens_at") else None,
            f"開演: {attributes.get('performance_starts_at')}"
            if attributes.get("performance_starts_at")
            else None,
            f"終演: {attributes.get('performance_ends_at')}"
            if attributes.get("performance_ends_at")
            else None,
            _plain_html(attributes.get("attention_body")),
            _plain_html(attributes.get("ticket_attention_body")),
        ]
        return "\n".join(str(value) for value in values if value)

    @staticmethod
    def _reception_content(
        reception: dict[str, Any],
        *,
        tour: dict[str, Any] | None,
        acts: list[dict[str, Any]],
        max_chars: int,
    ) -> str:
        attributes = reception.get("attributes") or {}
        tour_name = ((tour or {}).get("attributes") or {}).get("name")
        lines = [
            f"イベント: {tour_name}" if tour_name else None,
            f"受付名: {attributes.get('name')}" if attributes.get("name") else None,
            f"受付形式: {attributes.get('entry_type')}" if attributes.get("entry_type") else None,
            f"受付状態: {attributes.get('entry_period_status')}"
            if attributes.get("entry_period_status")
            else None,
            f"受付開始: {attributes.get('entry_period_starts_at')}"
            if attributes.get("entry_period_starts_at")
            else None,
            f"受付終了: {attributes.get('entry_period_ends_at')}"
            if attributes.get("entry_period_ends_at")
            else None,
            f"結果発表: {attributes.get('result_announcement_scheduled_at')}"
            if attributes.get("result_announcement_scheduled_at")
            else None,
            f"支払開始: {attributes.get('deposit_period_starts_at')}"
            if attributes.get("deposit_period_starts_at")
            else None,
            f"支払終了: {attributes.get('deposit_period_ends_at')}"
            if attributes.get("deposit_period_ends_at")
            else None,
        ]
        if acts:
            lines.append("公演候補:")
            for act in acts:
                act_attributes = act.get("attributes") or {}
                lines.append(
                    "- "
                    + json.dumps(
                        {
                            "id": act.get("id"),
                            "name": act_attributes.get("name"),
                            "venue": act_attributes.get("venue"),
                            "performanceDate": act_attributes.get("performance_date"),
                            "doorsAt": act_attributes.get("opens_at"),
                            "startsAt": act_attributes.get("performance_starts_at"),
                            "endsAt": act_attributes.get("performance_ends_at"),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
        for key in (
            "top_body",
            "main_body",
            "payment_method_description_body",
            "ticket_reception_method_description_body",
            "attention_body",
        ):
            body = _plain_html(attributes.get(key))
            if body:
                lines.extend((f"{key}:", body))
        return "\n".join(str(value) for value in lines if value)[:max_chars]

    def fetch(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = AsobiTicketConfig.model_validate(request.config)
        api_base = config.api_base_url.rstrip("/")
        site_base = config.site_base_url.rstrip("/")
        api_host = urlsplit(api_base).hostname or ""
        client = SafeHttpClient(
            user_agent="GenchiCollector/0.1 (+https://genchi.news)",
            timeout_seconds=60,
            retries=3,
            max_response_bytes=25_000_000,
            obey_robots=True,
            allow_private_network=False,
            allowed_hosts=(api_host,),
            rate_limit_seconds=0.25,
            headers={"Accept": "application/json"},
        )

        booths: list[dict[str, Any]] = []
        pages = 0
        for page in range(1, config.max_pages + 1):
            query = urlencode({"page": page, "per_page": config.per_page})
            payload = self._get(client, f"{api_base}/booths?{query}")
            items = payload.get("data") or []
            if not isinstance(items, list):
                raise ConfigurationError("ASOBI TICKET booths data must be a list")
            booths.extend(item for item in items if isinstance(item, dict))
            pages += 1
            if len(items) < config.per_page or len(booths) >= config.max_booths:
                break
        booths = booths[: config.max_booths]
        if not booths:
            raise TransientError("ASOBI TICKET returned no booths")

        reception_count = 0
        emitted_act_ids: set[str] = set()
        for booth_summary in booths:
            attributes = booth_summary.get("attributes") or {}
            slug = str(attributes.get("slug") or booth_summary.get("id") or "")
            if not slug:
                continue
            tags = _asobi_project(request.tags, booth_summary)
            booth_url = f"{site_base}/booths/{quote(slug, safe='')}"
            booth_payload = self._get(client, f"{api_base}/booths/{quote(slug, safe='')}")
            booth = booth_payload.get("data") or booth_summary
            booth_included = [
                item for item in booth_payload.get("included") or [] if isinstance(item, dict)
            ]
            booth_attributes = booth.get("attributes") or {}
            context.emit(
                ResourceRecord(
                    external_id=f"asobi:booth:{booth.get('id') or slug}",
                    kind="ticket_booth",
                    url=booth_url,
                    title=booth_attributes.get("name"),
                    content=_plain_html(booth_attributes.get("main_body"))[
                        : config.max_content_chars
                    ],
                    content_type="text/plain",
                    language="ja",
                    observed_at=datetime.now(UTC),
                    attributes={
                        "source_type": "asobi_ticket",
                        "asobi_ticket": {"booth": booth, "included": booth_included},
                        "media": _image_media(booth_included),
                    },
                    tags=tags,
                )
            )

            query = urlencode({"booth_slug": slug})
            receptions_payload = self._get(client, f"{api_base}/receptions?{query}")
            receptions = receptions_payload.get("data") or []
            if not isinstance(receptions, list):
                raise ConfigurationError("ASOBI TICKET receptions data must be a list")
            reception_count += len(receptions)
            included_items = [
                item for item in receptions_payload.get("included") or [] if isinstance(item, dict)
            ]
            included = _jsonapi_index(receptions_payload)
            acts = [item for item in included_items if item.get("type") == "act"]
            images = [item for item in included_items if item.get("type") == "image"]
            tour = next((item for item in included_items if item.get("type") == "tour"), None)

            for act in acts:
                act_id = str(act.get("id") or "")
                if not act_id or act_id in emitted_act_ids:
                    continue
                emitted_act_ids.add(act_id)
                act_attributes = act.get("attributes") or {}
                context.emit(
                    ResourceRecord(
                        external_id=f"asobi:act:{act_id}",
                        kind="ticket_act",
                        url=booth_url,
                        title=act_attributes.get("name"),
                        content=self._act_content(act, tour),
                        content_type="text/plain",
                        language="ja",
                        observed_at=datetime.now(UTC),
                        attributes={
                            "source_type": "asobi_ticket",
                            "asobi_ticket": {"act": act, "tour": tour, "booth": booth},
                            "media": _image_media(images),
                        },
                        tags=tags,
                    )
                )

            for reception in receptions:
                reception_id = str(reception.get("id") or "")
                if not reception_id:
                    continue
                reception_attributes = reception.get("attributes") or {}
                resale_act = _relationship(reception, "resale_act", included)
                related_acts = [resale_act] if resale_act else acts
                reception_url = f"{site_base}/receptions/{quote(reception_id, safe='')}"
                context.emit(
                    ResourceRecord(
                        external_id=f"asobi:reception:{reception_id}",
                        kind="ticket_reception",
                        url=reception_url,
                        title=reception_attributes.get("name"),
                        content=self._reception_content(
                            reception,
                            tour=tour,
                            acts=related_acts,
                            max_chars=config.max_content_chars,
                        ),
                        content_type="text/plain",
                        language="ja",
                        observed_at=datetime.now(UTC),
                        attributes={
                            "source_type": "asobi_ticket",
                            "asobi_ticket": {
                                "reception": reception,
                                "tour": tour,
                                "booth": booth,
                                "acts": related_acts,
                                "included": included_items,
                            },
                            "media": _image_media(images),
                        },
                        tags=tags,
                    )
                )

        context.set_checkpoint(
            {
                "last_success_at": datetime.now(UTC).isoformat(),
                "booths": len(booths),
                "receptions": reception_count,
                "acts": len(emitted_act_ids),
            }
        )
        return FetchReport(
            details={
                "pages": pages,
                "booths": len(booths),
                "receptions": reception_count,
                "acts": len(emitted_act_ids),
            }
        )


EPLUS_CATEGORY_URLS = (
    "https://eplus.jp/sf/anime/hokkaido-tohoku",
    "https://eplus.jp/sf/anime/kanto",
    "https://eplus.jp/sf/anime/hokushinetsu",
    "https://eplus.jp/sf/anime/tokai",
    "https://eplus.jp/sf/anime/kansai",
    "https://eplus.jp/sf/anime/chugoku-shikoku",
    "https://eplus.jp/sf/anime/kyushu-okinawa",
)

EPLUS_PROJECT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "idolmaster": (
        "アイドルマスター",
        "idolmaster",
        "idolm@ster",
        "シンデレラガールズ",
        "ミリオンライブ",
        "シャイニーカラーズ",
        "学園アイドルマスター",
        "side m",
        "sidem",
    ),
    "love-live": ("ラブライブ", "lovelive", "love live"),
    "bang-dream": ("bang dream", "bang dream!", "バンドリ", "夢限大みゅーたいぷ"),
    "girls-band-cry": ("ガールズバンドクライ", "girls band cry", "トゲナシトゲアリ"),
    "project-sekai": ("プロジェクトセカイ", "プロセカ", "project sekai"),
    "d4dj": ("d4dj",),
    "revue-starlight": ("少女☆歌劇", "レヴュースタァライト", "revue starlight"),
    "from-argonavis": ("from argonavis", "アルゴナビス"),
    "uma-musume": ("ウマ娘", "uma musume"),
    "idoly-pride": ("idoly pride", "アイドリープライド"),
    "tokyo-7th-sisters": ("tokyo 7th sisters", "ナナシス"),
    "22-7": ("22/7", "ナナブンノニジュウニ"),
    "denonbu": ("電音部", "denonbu"),
    "world-dai-star": ("ワールドダイスター", "world dai star"),
    "aikatsu": ("アイカツ", "aikatsu"),
    "pretty-series": ("プリティーシリーズ", "プリパラ", "プリティーリズム"),
    "macross": ("マクロス", "macross"),
    "symphogear": ("シンフォギア", "symphogear"),
    "bocchi-the-rock": ("ぼっち・ざ・ろっく", "ぼっちざろっく", "結束バンド"),
    "zombie-land-saga": ("ゾンビランドサガ", "zombieland saga"),
    "ensemble-stars": ("あんさんぶるスターズ", "あんスタ", "ensemble stars"),
    "idolish7": ("アイドリッシュセブン", "idolish7"),
    "hypnosis-mic": ("ヒプノシスマイク", "ヒプマイ", "hypnosis mic"),
    "uta-no-prince-sama": ("うたの☆プリンスさまっ", "うたプリ", "uta no prince"),
}

TICKET_PROJECT_QUERIES = tuple(
    dict.fromkeys(
        (
            "アニメ",
            "声優",
            "ゲーム",
            "アイドルマスター",
            "ラブライブ",
            "BanG Dream",
            "プロジェクトセカイ",
            "ウマ娘",
            "あんさんぶるスターズ",
            "アイドリッシュセブン",
            "ヒプノシスマイク",
            "うたの☆プリンスさまっ♪",
        )
    )
)

_EPLUS_DETAIL_RE = re.compile(r"^https://eplus\.jp/sf/detail/(?P<id>\d{10})(?:-[^?#/]+)?$")
_EPLUS_PERIOD_RE = re.compile(
    r"受付期間\s*[:：]\s*"
    r"(?P<start_year>\d{4})/(?P<start_month>\d{1,2})/(?P<start_day>\d{1,2})"
    r"(?:\([^)]*\))?\s*(?P<start_hour>\d{1,2}):(?P<start_minute>\d{2})"
    r"\s*[～〜~-]\s*"
    r"(?:(?P<end_year>\d{4})/(?P<end_month>\d{1,2})/(?P<end_day>\d{1,2})"
    r"(?:\([^)]*\))?\s*(?P<end_hour>\d{1,2}):(?P<end_minute>\d{2}))?"
)


def _eplus_normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def _eplus_display(value: Any) -> str:
    return unescape(str(value or "")).strip()


def _eplus_project(
    text: str, project_keywords: dict[str, tuple[str, ...]]
) -> tuple[str, list[str]]:
    normalized = _eplus_normalize(text)
    for project, keywords in project_keywords.items():
        matched = [keyword for keyword in keywords if _eplus_normalize(keyword) in normalized]
        if matched:
            return project, matched
    return "unknown", []


def _eplus_detail_url(base_url: str, href: str) -> tuple[str, str] | None:
    absolute = urljoin(base_url, href).split("?", 1)[0].split("#", 1)[0]
    match = _EPLUS_DETAIL_RE.fullmatch(absolute)
    if not match:
        return None
    page_id = match.group("id")
    return page_id, f"https://eplus.jp/sf/detail/{page_id}"


def _eplus_discover_details(html: str, page_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    details: dict[str, str] = {}
    for link in soup.select('a[href*="/sf/detail/"]'):
        parsed = _eplus_detail_url(page_url, str(link.get("href") or ""))
        if parsed:
            page_id, url = parsed
            details.setdefault(page_id, url)
    return list(details.items())


def _eplus_next_page(html: str, page_url: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one('link[rel="next"][href], a.block-paginator__nextprev--next[href]')
    if not node:
        return None
    target = urljoin(page_url, str(node.get("href") or ""))
    return target if target.startswith("https://eplus.jp/sf/anime/") else None


def _eplus_jsonld_events(soup: BeautifulSoup) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        kind = value.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "Event" in kinds:
            events.append(value)
        if "@graph" in value:
            visit(value["@graph"])

    for node in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(node.string or node.get_text()))
        except (TypeError, json.JSONDecodeError):
            continue
    unique: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(events):
        key = str(event.get("url") or f"{event.get('startDate')}:{event.get('name')}:{index}")
        unique.setdefault(key, event)
    return list(unique.values())


def _eplus_jst(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return f"{raw}T00:00:00+09:00"
    if re.search(r"(?:Z|[+-]\d{2}:\d{2})$", raw):
        return raw
    return f"{raw}+09:00"


def _eplus_timestamp(year: str, month: str, day: str, hour: str, minute: str) -> str:
    return (
        f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        f"T{int(hour):02d}:{int(minute):02d}:00+09:00"
    )


def _eplus_ticket_phase(label: str) -> str:
    normalized = _eplus_normalize(label)
    if "リセール" in normalized or "再販売" in normalized:
        return "RESALE"
    if "当日" in normalized:
        return "DAY_OF"
    if "fc" in normalized or "ファンクラブ" in normalized:
        return "FC_PRE"
    if "3次" in normalized:
        return "LOTTERY_3"
    if "2次" in normalized:
        return "LOTTERY_2"
    if "抽選" in normalized or "プレオーダー" in normalized:
        return "LOTTERY_1"
    if "先行" in normalized:
        return "ADVANCE"
    if "一般" in normalized or "先着" in normalized:
        return "GENERAL"
    return "OTHER"


def _eplus_ticket_status(text: str) -> str | None:
    normalized = _eplus_normalize(text)
    if "中止" in normalized:
        return "CANCELED"
    if "受付中" in normalized or "販売期間中" in normalized or "発売中" in normalized:
        return "OPEN"
    if "受付前" in normalized or "発売前" in normalized:
        return "UPCOMING"
    if "受付終了" in normalized or "販売終了" in normalized or "予定枚数終了" in normalized:
        return "CLOSED"
    return None


def _eplus_ticket_windows(article: Any) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for section in article.select("section.block-ticket"):
        label_node = section.select_one(".block-ticket__title")
        period_node = section.select_one(".block-ticket__time")
        label = label_node.get_text(" ", strip=True) if label_node else ""
        period = period_node.get_text(" ", strip=True) if period_node else ""
        match = _EPLUS_PERIOD_RE.search(period)
        if not label or not match:
            continue
        values = match.groupdict()
        opens_at = _eplus_timestamp(
            values["start_year"],
            values["start_month"],
            values["start_day"],
            values["start_hour"],
            values["start_minute"],
        )
        closes_at = None
        if values.get("end_year"):
            closes_at = _eplus_timestamp(
                values["end_year"],
                values["end_month"],
                values["end_day"],
                values["end_hour"],
                values["end_minute"],
            )
        digest = hashlib.sha256(
            f"{_eplus_normalize(label)}:{opens_at}:{closes_at or ''}".encode()
        ).hexdigest()[:16]
        windows.append(
            {
                "id": digest,
                "label": label,
                "phase": _eplus_ticket_phase(label),
                "opensAt": opens_at,
                "closesAt": closes_at,
                "status": _eplus_ticket_status(section.get_text(" ", strip=True)),
            }
        )
    return windows


def _eplus_doors_at(article: Any, starts_at: str | None) -> str | None:
    if not starts_at:
        return None
    match = re.search(r"開場\s*(\d{1,2}):(\d{2})", article.get_text(" ", strip=True))
    if not match:
        return None
    return f"{starts_at[:10]}T{int(match.group(1)):02d}:{match.group(2)}:00+09:00"


def _eplus_parse_detail(
    html: str,
    *,
    page_id: str,
    page_url: str,
    project_keywords: dict[str, tuple[str, ...]],
    category_discovered: bool,
    max_content_chars: int,
) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "lxml")
    if "混雑のお知らせ" in (soup.title.get_text(" ", strip=True) if soup.title else ""):
        raise TransientError("e+ returned its congestion page")
    jsonld_events = _eplus_jsonld_events(soup)
    if not jsonld_events:
        return None
    title = _eplus_display(jsonld_events[0].get("name") or _meta(soup, "og:title"))
    related = [
        node.get_text(" ", strip=True)
        for node in soup.select(".section--s4-breadcrumbs .breadcrumb-list__name")
        if node.get_text(" ", strip=True)
    ]
    project, matched_keywords = _eplus_project("\n".join([title, *related]), project_keywords)
    anime_genre = any(
        token in _eplus_normalize(" ".join(related))
        for token in ("アニメ", "ゲーム", "声優", "2.5")
    )
    if not category_discovered and not anime_genre and not matched_keywords:
        return None

    articles = soup.select("article.block-ticket-article")
    events: list[dict[str, Any]] = []
    for index, event in enumerate(jsonld_events):
        event_url = str(event.get("url") or page_url)
        variant = event_url.rsplit("/", 1)[-1]
        if not variant.startswith(page_id):
            variant = hashlib.sha256(
                f"{event.get('name')}:{event.get('startDate')}:{index}".encode()
            ).hexdigest()[:24]
        location = event.get("location") if isinstance(event.get("location"), dict) else {}
        address = location.get("address") if isinstance(location.get("address"), dict) else {}
        article = articles[index] if index < len(articles) else None
        windows = _eplus_ticket_windows(article) if article else []
        starts_at = _eplus_jst(event.get("startDate"))
        events.append(
            {
                "id": variant,
                "name": _eplus_display(event.get("name") or title),
                "url": event_url,
                "startsAt": starts_at,
                "endsAt": _eplus_jst(event.get("endDate")),
                "doorsAt": _eplus_doors_at(article, starts_at) if article else None,
                "venue": {
                    "name": _eplus_display(location.get("name")) or None,
                    "url": location.get("url"),
                    "prefecture": address.get("addressRegion"),
                    "country": address.get("addressCountry"),
                },
                "ticketWindows": windows,
            }
        )
    image = _meta(soup, "og:image", "twitter:image")
    content_lines = [f"イベント: {title}", f"関連ジャンル: {' / '.join(related)}"]
    for event in events:
        venue = event["venue"]
        content_lines.append(
            "公演: "
            + json.dumps(
                {
                    "id": event["id"],
                    "name": event["name"],
                    "startsAt": event["startsAt"],
                    "endsAt": event["endsAt"],
                    "doorsAt": event["doorsAt"],
                    "venue": venue.get("name"),
                    "prefecture": venue.get("prefecture"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        for window in event["ticketWindows"]:
            content_lines.append(
                "受付: " + json.dumps(window, ensure_ascii=False, separators=(",", ":"))
            )
    return {
        "title": title,
        "content": "\n".join(content_lines)[:max_content_chars],
        "project": project,
        "matchedKeywords": matched_keywords,
        "relatedGenres": related,
        "events": events,
        "media": [{"type": "image", "url": image}] if image else [],
    }


class EplusTicketConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category_urls: tuple[str, ...] = EPLUS_CATEGORY_URLS
    seed_urls: tuple[str, ...] = ()
    roots_per_run: int = Field(default=2, ge=1, le=7)
    pages_per_root: int = Field(default=2, ge=1, le=10)
    refresh_details_per_run: int = Field(default=20, ge=0, le=200)
    max_detail_pages: int = Field(default=80, ge=1, le=500)
    max_tracked_details: int = Field(default=2000, ge=50, le=10_000)
    max_content_chars: int = Field(default=120_000, ge=1000, le=500_000)
    rate_limit_seconds: float = Field(default=2.5, ge=1.0, le=60.0)
    browser_fallback: bool = True
    browser_url: str = "http://browser:3003"
    browser_token_secret: str = "BROWSER_API_TOKEN"
    project_keywords: dict[str, tuple[str, ...]] = Field(
        default_factory=lambda: dict(EPLUS_PROJECT_KEYWORDS)
    )

    @field_validator("category_urls")
    @classmethod
    def validate_category_urls(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("category_urls must not be empty")
        for url in value:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "eplus.jp"
                or not parsed.path.startswith("/sf/anime/")
            ):
                raise ValueError("category_urls must be public eplus.jp anime category URLs")
        return value

    @field_validator("seed_urls")
    @classmethod
    def validate_seed_urls(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for url in value:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "eplus.jp"
                or not re.fullmatch(r"/sf/word/\d+", parsed.path)
            ):
                raise ValueError("seed_urls must be public numeric eplus.jp word pages")
        return value


class EplusTicketFetcher(FetcherPlugin):
    manifest = FetcherManifest(
        name="genchi.eplus_ticket",
        version="0.1.0",
        operations=("fetch",),
        default_queue="browser",
        default_timeout_seconds=1800,
        capabilities=("browser_api",),
    )
    config_model = EplusTicketConfig

    @staticmethod
    def _html(
        client: SafeHttpClient,
        browser: BrowserClient | None,
        url: str,
        *,
        require_events: bool = False,
    ) -> str:
        direct_html: str | None = None
        try:
            response = client.get(url, allowed_content_types=("text/html",))
            direct_html = response.text
            if "混雑のお知らせ" in direct_html:
                raise TransientError("e+ returned its congestion page")
            if not require_events or _eplus_jsonld_events(BeautifulSoup(direct_html, "lxml")):
                return direct_html
        except TransientError:
            if not browser:
                raise
        if not browser:
            return direct_html or ""
        html, _ = browser.render(url, selector="body")
        if "混雑のお知らせ" in html:
            raise TransientError("e+ returned its congestion page")
        return html

    def fetch(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = EplusTicketConfig.model_validate(request.config)
        checkpoint = context.checkpoint()
        client = SafeHttpClient(
            user_agent="GenchiCollector/0.1 (+https://genchi.news)",
            timeout_seconds=60,
            retries=2,
            max_response_bytes=12_000_000,
            obey_robots=True,
            allow_private_network=False,
            allowed_hosts=("eplus.jp",),
            rate_limit_seconds=config.rate_limit_seconds,
            headers={"Accept-Language": "ja-JP,ja;q=0.9"},
        )
        token = (
            context.secret(config.browser_token_secret, required=False)
            if config.browser_fallback
            else None
        )
        browser = BrowserClient(config.browser_url, token) if token else None

        root_cursor = int(checkpoint.get("category_root_cursor") or 0) % len(config.category_urls)
        page_cursors = {
            str(key): str(value)
            for key, value in (checkpoint.get("category_page_cursors") or {}).items()
        }
        tracked = [
            str(value)
            for value in checkpoint.get("tracked_detail_urls") or []
            if _eplus_detail_url("https://eplus.jp", str(value))
        ][: config.max_tracked_details]
        refresh_cursor = int(checkpoint.get("refresh_cursor") or 0)
        candidates: dict[str, str] = {}
        discoveries: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        list_pages = 0
        seed_pages = 0

        def add_candidate(
            page_id: str,
            detail_url: str,
            *,
            kind: str,
            source_url: str,
            trusted_category: bool,
        ) -> None:
            candidates.setdefault(page_id, detail_url)
            discovery = {
                "kind": kind,
                "sourceUrl": source_url,
                "trustedCategory": trusted_category,
            }
            known = discoveries.setdefault(page_id, [])
            if discovery not in known:
                known.append(discovery)

        for seed_url in config.seed_urls:
            try:
                html = self._html(client, browser, seed_url)
            except TransientError as exc:
                errors.append(f"{seed_url}: {exc}")
                continue
            seed_pages += 1
            for page_id, detail_url in _eplus_discover_details(html, seed_url):
                add_candidate(
                    page_id,
                    detail_url,
                    kind="platform_word",
                    source_url=seed_url,
                    trusted_category=False,
                )

        selected_roots = [
            config.category_urls[(root_cursor + offset) % len(config.category_urls)]
            for offset in range(min(config.roots_per_run, len(config.category_urls)))
        ]
        for root in selected_roots:
            page_url = root
            discovered_next: str | None = None
            for page_index in range(config.pages_per_root):
                if page_index == 1:
                    page_url = page_cursors.get(root, f"{root}/p2")
                elif page_index > 1:
                    if not discovered_next:
                        break
                    page_url = discovered_next
                try:
                    html = self._html(client, browser, page_url)
                except TransientError as exc:
                    errors.append(f"{page_url}: {exc}")
                    break
                list_pages += 1
                for page_id, detail_url in _eplus_discover_details(html, page_url):
                    add_candidate(
                        page_id,
                        detail_url,
                        kind="platform_category",
                        source_url=page_url,
                        trusted_category=True,
                    )
                discovered_next = _eplus_next_page(html, page_url)
                if page_index >= 1:
                    page_cursors[root] = discovered_next or f"{root}/p2"
                if not discovered_next:
                    break

        if tracked and config.refresh_details_per_run:
            count = min(config.refresh_details_per_run, len(tracked))
            for offset in range(count):
                url = tracked[(refresh_cursor + offset) % len(tracked)]
                parsed = _eplus_detail_url("https://eplus.jp", url)
                if parsed:
                    add_candidate(
                        parsed[0],
                        parsed[1],
                        kind="refresh",
                        source_url=url,
                        trusted_category=False,
                    )
            refresh_cursor = (refresh_cursor + count) % len(tracked)

        if not candidates and errors:
            raise TransientError(f"all e+ discovery pages failed: {errors[0]}")

        emitted = 0
        rejected = 0
        event_count = 0
        ticket_count = 0
        tracked_set = dict.fromkeys(tracked)
        for page_id, detail_url in list(candidates.items())[: config.max_detail_pages]:
            discovery = discoveries.get(page_id, [])
            category_discovered = any(bool(item.get("trustedCategory")) for item in discovery)
            try:
                html = self._html(client, browser, detail_url, require_events=True)
                parsed = _eplus_parse_detail(
                    html,
                    page_id=page_id,
                    page_url=detail_url,
                    project_keywords=config.project_keywords,
                    category_discovered=category_discovered,
                    max_content_chars=config.max_content_chars,
                )
            except TransientError as exc:
                errors.append(f"{detail_url}: {exc}")
                continue
            if not parsed:
                rejected += 1
                continue
            project = str(parsed["project"])
            tags = tuple(tag for tag in request.tags if not str(tag).startswith("project:")) + (
                f"project:{project}",
            )
            events = parsed["events"]
            context.emit(
                ResourceRecord(
                    external_id=f"eplus:detail:{page_id}",
                    kind="eplus_ticket_page",
                    url=detail_url,
                    title=parsed["title"],
                    content=parsed["content"],
                    content_type="text/plain",
                    language="ja",
                    observed_at=datetime.now(UTC),
                    attributes={
                        "source_type": "eplus_ticket",
                        "eplus_ticket": {
                            "pageId": page_id,
                            "project": project,
                            "matchedKeywords": parsed["matchedKeywords"],
                            "relatedGenres": parsed["relatedGenres"],
                            "nativeCategories": parsed["relatedGenres"],
                            "discovery": discovery,
                            "events": events,
                        },
                        "media": parsed["media"],
                    },
                    tags=tags,
                )
            )
            tracked_set[detail_url] = None
            emitted += 1
            event_count += len(events)
            ticket_count += sum(len(event["ticketWindows"]) for event in events)

        tracked = list(tracked_set)[-config.max_tracked_details :]
        context.set_checkpoint(
            {
                "last_success_at": datetime.now(UTC).isoformat(),
                "category_root_cursor": (root_cursor + len(selected_roots))
                % len(config.category_urls),
                "category_page_cursors": page_cursors,
                "tracked_detail_urls": tracked,
                "refresh_cursor": refresh_cursor,
            }
        )
        return FetchReport(
            status="partial" if errors else "succeeded",
            details={
                "list_pages": list_pages,
                "seed_pages": seed_pages,
                "candidates": len(candidates),
                "details": emitted,
                "rejected": rejected,
                "events": event_count,
                "ticket_windows": ticket_count,
                "tracked": len(tracked),
                "errors": errors[:10],
            },
        )


PIA_DISCOVERY_URLS = (
    "https://t.pia.jp/anime/",
    "https://t.pia.jp/pia/tag/tag.do?tagCd=0000037",
)
_PIA_EVENT_PATH = "/pia/event/event.do"
_PIA_SALE_PATH = "/pia/ticketInformation.do"
_JAPANESE_DATE_TIME_RE = re.compile(
    r"(?P<year>20\d{2})[/-](?P<month>\d{1,2})[/-](?P<day>\d{1,2})"
    r"(?:\([^)]*\))?\s*(?:(?P<period>午前|午後|昼|夜)\s*)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?"
)
_JAPANESE_DATE_RE = re.compile(r"(?P<year>20\d{2})[/-](?P<month>\d{1,2})[/-](?P<day>\d{1,2})")


def _response_html(response: requests.Response) -> str:
    content = response.content
    encoding = str(getattr(response, "encoding", None) or "utf-8")
    try:
        decoded = content.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        decoded = content.decode("utf-8", errors="replace")
    return str(BeautifulSoup(decoded, "lxml"))


def _pia_ticket_phase(label: str, page_text: str) -> str:
    phase = _eplus_ticket_phase(label)
    return phase if phase != "OTHER" else _eplus_ticket_phase(page_text[:500])


def _ticket_timestamps(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value)
    timestamps: list[str] = []
    for match in _JAPANESE_DATE_TIME_RE.finditer(normalized):
        hour = int(match.group("hour"))
        period = match.group("period")
        if period in {"午後", "夜"} and hour < 12:
            hour += 12
        elif period == "午前" and hour == 12:
            hour = 0
        elif period == "昼" and hour < 10:
            hour += 12
        timestamps.append(
            _eplus_timestamp(
                match.group("year"),
                match.group("month"),
                match.group("day"),
                str(hour),
                match.group("minute") or "00",
            )
        )
    return list(dict.fromkeys(timestamps))


def _ticket_date(value: str, *, hour: int = 0, minute: int = 0) -> str | None:
    match = _JAPANESE_DATE_RE.search(unicodedata.normalize("NFKC", value))
    if not match:
        return None
    return _eplus_timestamp(
        match.group("year"),
        match.group("month"),
        match.group("day"),
        str(hour),
        str(minute),
    )


def _definition_value(soup: BeautifulSoup, label: str) -> str:
    for node in soup.select("dl.dataList"):
        title = node.select_one("dt")
        detail = node.select_one("dd")
        if title and detail and label in title.get_text(" ", strip=True):
            return detail.get_text(" ", strip=True)
    return ""


def _pia_detail_url(base_url: str, href: str) -> tuple[str, str] | None:
    absolute = urljoin(base_url, href)
    parsed = urlsplit(absolute)
    if parsed.hostname not in {"t.pia.jp", "www.t.pia.jp"} or parsed.path != _PIA_EVENT_PATH:
        return None
    query = parse_qs(parsed.query)
    for key in ("eventBundleCd", "eventCd"):
        value = str((query.get(key) or [""])[0]).strip()
        if re.fullmatch(r"[A-Za-z0-9]+", value):
            return value, f"https://t.pia.jp{_PIA_EVENT_PATH}?{key}={quote(value)}"
    return None


def _pia_discover_details(html: str, page_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    details: dict[str, str] = {}
    for link in soup.select('a[href*="/pia/event/event.do"]'):
        parsed = _pia_detail_url(page_url, str(link.get("href") or ""))
        if parsed:
            details.setdefault(parsed[0], parsed[1])
    return list(details.items())


def _pia_sale_id(url: str) -> str:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    parts = [
        f"{key}-{str((query.get(key) or [''])[0])}"
        for key in ("eventCd", "rlsCd", "lotRlsCd")
        if query.get(key)
    ]
    return "-".join(parts) or hashlib.sha256(url.encode()).hexdigest()[:20]


def _pia_sale_links(html: str, page_url: str, *, limit: int) -> list[tuple[str, str, str, str]]:
    soup = BeautifulSoup(html, "lxml")
    links: dict[str, tuple[str, str, str, str]] = {}
    for card in soup.select(".ticketSalesCard-2024"):
        link = card.select_one(f'a[href*="{_PIA_SALE_PATH}"]')
        if not link:
            continue
        url = urljoin(page_url, str(link.get("href") or ""))
        parsed = urlsplit(url)
        if parsed.hostname not in {"t.pia.jp", "www.t.pia.jp"} or parsed.path != _PIA_SALE_PATH:
            continue
        label_node = card.select_one(".ticketSalesCard-2024__title")
        status_node = card.select_one(".ticketSalesCard-2024__status")
        label = label_node.get_text(" ", strip=True) if label_node else ""
        status = status_node.get_text(" ", strip=True) if status_node else ""
        sale_id = _pia_sale_id(url)
        links.setdefault(sale_id, (sale_id, url.replace("http://", "https://"), label, status))
    values = list(links.values())
    values.sort(
        key=lambda item: (
            1 if _eplus_ticket_status(item[3]) == "CLOSED" else 0,
            item[0],
        )
    )
    return values[:limit]


def _pia_window(
    soup: BeautifulSoup,
    *,
    sale_id: str,
    sale_url: str,
    fallback_label: str,
) -> dict[str, Any] | None:
    label_node = soup.select_one(".textLabel--title")
    label = (label_node.get_text(" ", strip=True) if label_node else fallback_label).strip()
    page_text = soup.get_text(" ", strip=True)
    period = _definition_value(soup, "受付期間")
    values = _ticket_timestamps(period)
    opens_at = values[0] if values else None
    closes_at = values[1] if len(values) > 1 else None
    if not opens_at:
        starts = _ticket_timestamps(_definition_value(soup, "発売開始"))
        opens_at = starts[0] if starts else None
    if not closes_at:
        status_node = soup.select_one(".textLabel")
        status_text = unicodedata.normalize(
            "NFKC",
            status_node.get_text(" ", strip=True) if status_node else "",
        )
        close_match = re.search(
            r"[～〜~]\s*(20\d{2}/\d{1,2}/\d{1,2}[^～〜~]*?\d{1,2}(?::\d{2})?)",
            status_text,
        )
        status_values = _ticket_timestamps(close_match.group(1) if close_match else "")
        closes_at = status_values[-1] if status_values else None
    if not opens_at:
        return None
    result_values = _ticket_timestamps(_definition_value(soup, "結果発表開始日時"))
    window_id = hashlib.sha256(
        (
            f"{_eplus_normalize(label or fallback_label)}:{opens_at}:"
            f"{closes_at or ''}:{result_values[0] if result_values else ''}"
        ).encode()
    ).hexdigest()[:20]
    return {
        "id": window_id,
        "upstreamId": sale_id,
        "label": label or fallback_label or "チケット受付",
        "phase": _pia_ticket_phase(label, page_text),
        "opensAt": opens_at,
        "closesAt": closes_at,
        "resultAt": result_values[0] if result_values else None,
        "status": _eplus_ticket_status(page_text),
        "url": sale_url,
    }


def _pia_performances(
    soup: BeautifulSoup,
    *,
    title: str,
    page_url: str,
    window: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, section in enumerate(soup.select(".Y15-regular-section")):
        date_node = section.select_one(".Y15-event-date")
        time_node = section.select_one(".Y15-event-time")
        venue_node = section.select_one(".Y15-event-site-place")
        if not date_node:
            continue
        date_text = date_node.get_text(" ", strip=True)
        time_text = time_node.get_text(" ", strip=True) if time_node else ""
        start_match = re.search(r"(\d{1,2}):(\d{2})\s*開演", time_text)
        door_match = re.search(r"(\d{1,2}):(\d{2})\s*開場", time_text)
        starts_at = _ticket_date(
            date_text,
            hour=int(start_match.group(1)) if start_match else 0,
            minute=int(start_match.group(2)) if start_match else 0,
        )
        if not starts_at:
            continue
        doors_at = (
            _ticket_date(
                date_text,
                hour=int(door_match.group(1)),
                minute=int(door_match.group(2)),
            )
            if door_match
            else None
        )
        event_cd = str((section.select_one("input.eventCd[value]") or {}).get("value") or "")
        perf_cd = str((section.select_one("input.perfCd[value]") or {}).get("value") or "")
        performance_id = "-".join(value for value in (event_cd, perf_cd) if value)
        if not performance_id:
            performance_id = hashlib.sha256(f"{title}:{starts_at}:{index}".encode()).hexdigest()[
                :24
            ]
        venue_text = venue_node.get_text(" ", strip=True) if venue_node else ""
        venue_text = re.sub(r"^会場\s*[:：]\s*", "", venue_text)
        prefecture_match = re.search(r"[（(]([^()（）]+?[都道府県])[）)]", venue_text)
        venue_name = re.sub(r"\s*[（(][^()（）]+?[都道府県][）)]\s*$", "", venue_text).strip()
        events.append(
            {
                "id": performance_id,
                "name": title,
                "url": page_url,
                "startsAt": starts_at,
                "endsAt": None,
                "doorsAt": doors_at,
                "venue": {
                    "name": venue_name or None,
                    "url": None,
                    "prefecture": prefecture_match.group(1) if prefecture_match else None,
                    "country": "日本",
                },
                "ticketWindows": [window] if window else [],
            }
        )
    return events


def _pia_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    title = _meta(soup, "og:title") or ""
    if not title:
        node = soup.select_one("h1")
        title = node.get_text(" ", strip=True) if node else ""
    return re.split(r"\s*[|｜]\s*チケットぴあ", _eplus_display(title))[0].strip()


class PiaTicketConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    discovery_urls: tuple[str, ...] = PIA_DISCOVERY_URLS
    search_keywords: tuple[str, ...] = TICKET_PROJECT_QUERIES
    keywords_per_run: int = Field(default=4, ge=0, le=30)
    refresh_details_per_run: int = Field(default=10, ge=0, le=100)
    max_detail_pages: int = Field(default=40, ge=1, le=200)
    max_sales_per_detail: int = Field(default=12, ge=1, le=50)
    max_tracked_details: int = Field(default=1500, ge=20, le=10_000)
    max_content_chars: int = Field(default=120_000, ge=1000, le=500_000)
    rate_limit_seconds: float = Field(default=2.0, ge=1.0, le=60.0)
    browser_fallback: bool = True
    browser_url: str = "http://browser:3003"
    browser_token_secret: str = "BROWSER_API_TOKEN"
    project_keywords: dict[str, tuple[str, ...]] = Field(
        default_factory=lambda: dict(EPLUS_PROJECT_KEYWORDS)
    )

    @field_validator("discovery_urls")
    @classmethod
    def validate_discovery_urls(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("discovery_urls must not be empty")
        for url in value:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname != "t.pia.jp":
                raise ValueError("discovery_urls must be public t.pia.jp URLs")
        return value


class PiaTicketFetcher(FetcherPlugin):
    manifest = FetcherManifest(
        name="genchi.pia_ticket",
        version="0.1.0",
        operations=("fetch",),
        default_queue="browser",
        default_timeout_seconds=1800,
        capabilities=("browser_api",),
    )
    config_model = PiaTicketConfig

    @staticmethod
    def _html(
        client: SafeHttpClient,
        browser: BrowserClient | None,
        url: str,
        *,
        selector: str | None = None,
    ) -> str:
        direct_html: str | None = None
        try:
            response = client.get(url, allowed_content_types=("text/html",))
            if urlsplit(response.url).hostname == "sorry.pia.jp":
                raise TransientError("Ticket Pia returned its congestion page")
            direct_html = _response_html(response)
            if not selector or BeautifulSoup(direct_html, "lxml").select_one(selector):
                return direct_html
        except TransientError:
            if not browser:
                raise
        if not browser:
            return direct_html or ""
        html, final_url = browser.render(url, selector=selector or "body")
        if urlsplit(final_url).hostname == "sorry.pia.jp":
            raise TransientError("Ticket Pia browser reached its congestion page")
        return html

    def fetch(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = PiaTicketConfig.model_validate(request.config)
        checkpoint = context.checkpoint()
        client = SafeHttpClient(
            user_agent="GenchiCollector/0.1 (+https://genchi.news)",
            timeout_seconds=60,
            retries=2,
            max_response_bytes=12_000_000,
            obey_robots=True,
            allow_private_network=False,
            allowed_hosts=("t.pia.jp", "sorry.pia.jp"),
            rate_limit_seconds=config.rate_limit_seconds,
            headers={"Accept-Language": "ja-JP,ja;q=0.9"},
        )
        token = (
            context.secret(config.browser_token_secret, required=False)
            if config.browser_fallback
            else None
        )
        browser = BrowserClient(config.browser_url, token) if token else None
        keyword_cursor = int(checkpoint.get("keyword_cursor") or 0)
        tracked = [
            str(value)
            for value in checkpoint.get("tracked_detail_urls") or []
            if _pia_detail_url("https://t.pia.jp", str(value))
        ][: config.max_tracked_details]
        refresh_cursor = int(checkpoint.get("refresh_cursor") or 0)
        candidates: dict[str, str] = {}
        discoveries: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        discovery_pages = 0
        sale_pages = 0

        def add_candidate(
            page_id: str,
            detail_url: str,
            *,
            kind: str,
            source_url: str,
            search_query: str | None = None,
            trusted_category: bool = False,
        ) -> None:
            candidates.setdefault(page_id, detail_url)
            discovery: dict[str, Any] = {
                "kind": kind,
                "sourceUrl": source_url,
                "trustedCategory": trusted_category,
            }
            if search_query:
                discovery["searchQuery"] = search_query
            known = discoveries.setdefault(page_id, [])
            if discovery not in known:
                known.append(discovery)

        trusted_discovery_urls = {
            url
            for url in config.discovery_urls
            if urlsplit(url).path.startswith("/anime/")
            or (
                urlsplit(url).path == "/pia/tag/tag.do"
                and "0000037" in parse_qs(urlsplit(url).query).get("tagCd", [])
            )
        }
        discovery_urls: list[tuple[str, str, str | None, bool]] = [
            (
                url,
                "platform_category",
                None,
                url in trusted_discovery_urls,
            )
            for url in config.discovery_urls
        ]
        if config.search_keywords and config.keywords_per_run:
            count = min(config.keywords_per_run, len(config.search_keywords))
            selected = [
                config.search_keywords[(keyword_cursor + offset) % len(config.search_keywords)]
                for offset in range(count)
            ]
            discovery_urls.extend(
                (
                    f"https://t.pia.jp/pia/search_all.do?{urlencode({'kw': keyword})}",
                    "search",
                    keyword,
                    False,
                )
                for keyword in selected
            )
            keyword_cursor = (keyword_cursor + count) % len(config.search_keywords)
        for url, discovery_kind, search_query, trusted_category in discovery_urls:
            try:
                html = self._html(client, browser, url)
            except TransientError as exc:
                errors.append(f"{url}: {exc}")
                continue
            discovery_pages += 1
            for page_id, detail_url in _pia_discover_details(html, url):
                add_candidate(
                    page_id,
                    detail_url,
                    kind=discovery_kind,
                    source_url=url,
                    search_query=search_query,
                    trusted_category=trusted_category,
                )
        if tracked and config.refresh_details_per_run:
            count = min(config.refresh_details_per_run, len(tracked))
            for offset in range(count):
                parsed = _pia_detail_url(
                    "https://t.pia.jp",
                    tracked[(refresh_cursor + offset) % len(tracked)],
                )
                if parsed:
                    add_candidate(
                        parsed[0],
                        parsed[1],
                        kind="refresh",
                        source_url=tracked[(refresh_cursor + offset) % len(tracked)],
                    )
            refresh_cursor = (refresh_cursor + count) % len(tracked)
        if not candidates and errors:
            raise TransientError(f"all Ticket Pia discovery pages failed: {errors[0]}")

        emitted = 0
        event_count = 0
        ticket_count = 0
        tracked_set = dict.fromkeys(tracked)
        for page_id, detail_url in list(candidates.items())[: config.max_detail_pages]:
            try:
                detail_html = self._html(client, browser, detail_url)
            except TransientError as exc:
                errors.append(f"{detail_url}: {exc}")
                continue
            title = _pia_title(detail_html)
            if not title:
                continue
            project, matched_keywords = _eplus_project(
                f"{title}\n{BeautifulSoup(detail_html, 'lxml').get_text(' ', strip=True)}",
                config.project_keywords,
            )
            merged_events: dict[str, dict[str, Any]] = {}
            sales = _pia_sale_links(
                detail_html,
                detail_url,
                limit=config.max_sales_per_detail,
            )
            for sale_id, sale_url, sale_label, _sale_status in sales:
                try:
                    sale_html = self._html(
                        client,
                        browser,
                        sale_url,
                        selector=".Y15-regular-section",
                    )
                except TransientError as exc:
                    errors.append(f"{sale_url}: {exc}")
                    continue
                sale_pages += 1
                sale_soup = BeautifulSoup(sale_html, "lxml")
                window = _pia_window(
                    sale_soup,
                    sale_id=sale_id,
                    sale_url=sale_url,
                    fallback_label=sale_label,
                )
                for event in _pia_performances(
                    sale_soup,
                    title=title,
                    page_url=detail_url,
                    window=window,
                ):
                    existing = merged_events.get(event["id"])
                    if not existing:
                        merged_events[event["id"]] = event
                    elif window and all(
                        item["id"] != window["id"] for item in existing["ticketWindows"]
                    ):
                        existing["ticketWindows"].append(window)
            events = list(merged_events.values())
            if not events:
                continue
            content_lines = [f"イベント: {title}"]
            for event in events:
                content_lines.append(
                    "公演: " + json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                )
            tags = tuple(tag for tag in request.tags if not str(tag).startswith("project:")) + (
                f"project:{project}",
            )
            image = _meta(BeautifulSoup(detail_html, "lxml"), "og:image", "twitter:image")
            context.emit(
                ResourceRecord(
                    external_id=f"pia:detail:{page_id}",
                    kind="pia_ticket_page",
                    url=detail_url,
                    title=title,
                    content="\n".join(content_lines)[: config.max_content_chars],
                    content_type="text/plain",
                    language="ja",
                    observed_at=datetime.now(UTC),
                    attributes={
                        "source_type": "pia_ticket",
                        "ticket_page": {
                            "platform": "pia",
                            "pageId": page_id,
                            "project": project,
                            "matchedKeywords": matched_keywords,
                            "nativeCategories": [],
                            "discovery": discoveries.get(page_id, []),
                            "events": events,
                        },
                        "media": [{"type": "image", "url": image}] if image else [],
                    },
                    tags=tags,
                )
            )
            tracked_set[detail_url] = None
            emitted += 1
            event_count += len(events)
            ticket_count += sum(len(event["ticketWindows"]) for event in events)
        context.set_checkpoint(
            {
                "last_success_at": datetime.now(UTC).isoformat(),
                "keyword_cursor": keyword_cursor,
                "tracked_detail_urls": list(tracked_set)[-config.max_tracked_details :],
                "refresh_cursor": refresh_cursor,
            }
        )
        return FetchReport(
            status="partial" if errors else "succeeded",
            details={
                "discovery_pages": discovery_pages,
                "candidates": len(candidates),
                "details": emitted,
                "sale_pages": sale_pages,
                "events": event_count,
                "ticket_windows": ticket_count,
                "tracked": min(len(tracked_set), config.max_tracked_details),
                "errors": errors[:10],
            },
        )


def _lawson_dates(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value)
    return list(
        dict.fromkeys(
            f"{match.group('year')}-{int(match.group('month')):02d}-{int(match.group('day')):02d}"
            for match in _JAPANESE_DATE_RE.finditer(normalized)
        )
    )


def _lawson_parse_results(
    html: str,
    *,
    page_url: str,
    search_query: str,
    project_keywords: dict[str, tuple[str, ...]],
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    results: list[dict[str, Any]] = []
    for box in soup.select(".ResultBox"):
        title_node = box.select_one(".ResultBox__title")
        tables = box.select(".ResultBox__table.prfItem")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if not title or not tables:
            continue
        info: dict[str, str] = {}
        for row in box.select(".ResultBox__information"):
            key_node = row.select_one(".ResultBox__informationTitle")
            value_node = row.select_one(".ResultBox__informationText")
            if key_node and value_node:
                info[key_node.get_text(" ", strip=True).rstrip("：:")] = value_node.get_text(
                    " ", strip=True
                )
        venue_name = info.get("会場", "")
        category_node = box.select_one(".ResultBox__type")
        category = category_node.get_text(" ", strip=True) if category_node else ""
        project, matched_keywords = _eplus_project(f"{title}\n{category}", project_keywords)
        event_map: dict[str, dict[str, Any]] = {}
        lcodes: list[str] = []
        for table in tables:
            entry = table.select_one(".entryBtn")
            attrs = entry.attrs if entry else {}
            lcode = str(attrs.get("data-lcode") or "").strip()
            if lcode:
                lcodes.append(lcode)
            if attrs.get("data-basevenuename"):
                venue_name = str(attrs["data-basevenuename"]).strip()
            dates = [
                f"{value[:4]}-{value[4:6]}-{value[6:8]}"
                for value in str(attrs.get("data-prfdate") or "").split(",")
                if re.fullmatch(r"20\d{6}", value)
            ]
            dates = list(dict.fromkeys(dates)) or _lawson_dates(info.get("公演日", ""))
            label_parts = [
                node.get_text(" ", strip=True)
                for node in (
                    table.select_one("#sale_name"),
                    table.select_one("#reception_typename"),
                )
                if node and node.get_text(" ", strip=True)
            ]
            label = " ".join(dict.fromkeys(label_parts)) or "チケット受付"
            period_node = table.select_one("#receiptDat, .orderEndDate")
            period = period_node.get_text(" ", strip=True) if period_node else ""
            timestamps = _ticket_timestamps(period)
            if not timestamps:
                continue
            window_id = hashlib.sha256(
                f"{lcode}:{_eplus_normalize(label)}:{timestamps[0]}".encode()
            ).hexdigest()[:20]
            window = {
                "id": window_id,
                "label": label,
                "phase": _eplus_ticket_phase(label),
                "opensAt": timestamps[0],
                "closesAt": timestamps[1] if len(timestamps) > 1 else None,
                "resultAt": None,
                "status": _eplus_ticket_status(table.get_text(" ", strip=True)),
                "url": page_url,
            }
            for date in dates:
                performance_id = hashlib.sha256(
                    f"{_eplus_normalize(title)}:{date}:{_eplus_normalize(venue_name)}".encode()
                ).hexdigest()[:24]
                event = event_map.setdefault(
                    performance_id,
                    {
                        "id": performance_id,
                        "name": title,
                        "url": page_url,
                        "startsAt": f"{date}T00:00:00+09:00",
                        "endsAt": None,
                        "doorsAt": None,
                        "venue": {
                            "name": venue_name or None,
                            "url": None,
                            "prefecture": (
                                re.search(r"[（(]([^()（）]+?[都道府県])[）)]", venue_name).group(1)
                                if re.search(r"[（(]([^()（）]+?[都道府県])[）)]", venue_name)
                                else None
                            ),
                            "country": "日本",
                        },
                        "ticketWindows": [],
                    },
                )
                current_window = next(
                    (item for item in event["ticketWindows"] if item["id"] == window_id),
                    None,
                )
                if not current_window:
                    event["ticketWindows"].append(window)
                elif (window.get("closesAt") or "") > (current_window.get("closesAt") or ""):
                    current_window["closesAt"] = window.get("closesAt")
        events = list(event_map.values())
        if not events:
            continue
        page_id = (
            "-".join(sorted(set(lcodes)))
            or hashlib.sha256(
                f"{_eplus_normalize(title)}:{_eplus_normalize(venue_name)}".encode()
            ).hexdigest()[:20]
        )
        results.append(
            {
                "pageId": page_id,
                "title": title,
                "project": project,
                "matchedKeywords": matched_keywords,
                "category": category,
                "nativeCategories": [category] if category else [],
                "discovery": [
                    {
                        "kind": "search",
                        "sourceUrl": page_url,
                        "searchQuery": search_query,
                        "trustedCategory": False,
                    }
                ],
                "events": events,
            }
        )
    return results


class LawsonTicketConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    search_keywords: tuple[str, ...] = TICKET_PROJECT_QUERIES
    queries_per_run: int = Field(default=6, ge=1, le=30)
    max_results_per_run: int = Field(default=200, ge=1, le=1000)
    max_content_chars: int = Field(default=120_000, ge=1000, le=500_000)
    browser_url: str = "http://browser:3003"
    browser_token_secret: str = "BROWSER_API_TOKEN"
    project_keywords: dict[str, tuple[str, ...]] = Field(
        default_factory=lambda: dict(EPLUS_PROJECT_KEYWORDS)
    )

    @field_validator("search_keywords")
    @classmethod
    def validate_search_keywords(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(keyword.strip() for keyword in value if keyword.strip())
        if not cleaned:
            raise ValueError("search_keywords must not be empty")
        if any(len(keyword) > 100 for keyword in cleaned):
            raise ValueError("search keywords must not exceed 100 characters")
        return cleaned


class LawsonTicketFetcher(FetcherPlugin):
    manifest = FetcherManifest(
        name="genchi.lawson_ticket",
        version="0.1.0",
        operations=("fetch",),
        default_queue="browser",
        default_timeout_seconds=1800,
        capabilities=("browser_api",),
    )
    config_model = LawsonTicketConfig

    def fetch(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = LawsonTicketConfig.model_validate(request.config)
        checkpoint = context.checkpoint()
        browser = BrowserClient(
            config.browser_url,
            context.secret(config.browser_token_secret),
        )
        cursor = int(checkpoint.get("keyword_cursor") or 0) % len(config.search_keywords)
        count = min(config.queries_per_run, len(config.search_keywords))
        selected = [
            config.search_keywords[(cursor + offset) % len(config.search_keywords)]
            for offset in range(count)
        ]
        parsed_results: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        pages = 0
        for keyword in selected:
            url = f"https://l-tike.com/search/?{urlencode({'keyword': keyword})}"
            try:
                html, final_url = browser.render(url, selector="#layout_search_result")
            except TransientError as exc:
                errors.append(f"{url}: {exc}")
                continue
            pages += 1
            for parsed in _lawson_parse_results(
                html,
                page_url=final_url,
                search_query=keyword,
                project_keywords=config.project_keywords,
            ):
                page_id = str(parsed["pageId"])
                existing = parsed_results.get(page_id)
                if not existing:
                    parsed_results[page_id] = parsed
                    continue
                for discovery in parsed["discovery"]:
                    if discovery not in existing["discovery"]:
                        existing["discovery"].append(discovery)
                existing_events = {event["id"]: event for event in existing["events"]}
                for event in parsed["events"]:
                    current = existing_events.get(event["id"])
                    if not current:
                        existing["events"].append(event)
                        existing_events[event["id"]] = event
                        continue
                    known_windows = {item["id"] for item in current["ticketWindows"]}
                    current["ticketWindows"].extend(
                        item for item in event["ticketWindows"] if item["id"] not in known_windows
                    )
        if not parsed_results and errors:
            raise TransientError(f"all Lawson Ticket searches failed: {errors[0]}")
        emitted = 0
        event_count = 0
        ticket_count = 0
        for parsed in list(parsed_results.values())[: config.max_results_per_run]:
            project = str(parsed["project"])
            events = parsed["events"]
            tags = tuple(tag for tag in request.tags if not str(tag).startswith("project:")) + (
                f"project:{project}",
            )
            content = "\n".join(
                [
                    f"イベント: {parsed['title']}",
                    f"ジャンル: {parsed['category']}",
                    *[
                        "公演: " + json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                        for event in events
                    ],
                ]
            )
            context.emit(
                ResourceRecord(
                    external_id=f"lawson:result:{parsed['pageId']}",
                    kind="lawson_ticket_page",
                    url=events[0]["url"],
                    title=parsed["title"],
                    content=content[: config.max_content_chars],
                    content_type="text/plain",
                    language="ja",
                    observed_at=datetime.now(UTC),
                    attributes={
                        "source_type": "lawson_ticket",
                        "ticket_page": {
                            "platform": "lawson",
                            "pageId": parsed["pageId"],
                            "project": project,
                            "matchedKeywords": parsed["matchedKeywords"],
                            "nativeCategories": parsed["nativeCategories"],
                            "discovery": parsed["discovery"],
                            "events": events,
                        },
                        "media": [],
                    },
                    tags=tags,
                )
            )
            emitted += 1
            event_count += len(events)
            ticket_count += sum(len(event["ticketWindows"]) for event in events)
        context.set_checkpoint(
            {
                "last_success_at": datetime.now(UTC).isoformat(),
                "keyword_cursor": (cursor + count) % len(config.search_keywords),
            }
        )
        return FetchReport(
            status="partial" if errors else "succeeded",
            details={
                "search_pages": pages,
                "queries": selected,
                "results": emitted,
                "events": event_count,
                "ticket_windows": ticket_count,
                "errors": errors[:10],
            },
        )


class OfficialSiteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_urls: tuple[str, ...]
    link_pattern: str
    list_item_selector: str | None = None
    detail_link_selector: str = "a[href]"
    next_selector: str | None = None
    title_selector: str | None = "h1"
    content_selector: str = "article"
    published_selector: str | None = "time"
    browser: bool = False
    browser_url: str = "http://browser:3003"
    browser_token_secret: str = "BROWSER_API_TOKEN"
    wait_selector: str | None = None
    max_pages: int = Field(default=2, ge=1, le=20)
    backfill_max_pages: int = Field(default=30, ge=1, le=100)
    max_items: int = Field(default=100, ge=1, le=5000)

    @field_validator("start_urls")
    @classmethod
    def require_urls(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("start_urls cannot be empty")
        return value


class OfficialSiteFetcher(FetcherPlugin):
    manifest = FetcherManifest(
        name="genchi.official_site",
        version="0.1.0",
        operations=("fetch", "backfill"),
        default_queue="web",
        default_timeout_seconds=1200,
    )
    config_model = OfficialSiteConfig

    def _run(
        self,
        context: FetchContext,
        request: FetchRequest,
        *,
        max_pages: int,
    ) -> FetchReport:
        config = OfficialSiteConfig.model_validate(request.config)
        pattern = re.compile(config.link_pattern)
        allowed_hosts = tuple(sorted({urlsplit(url).hostname or "" for url in config.start_urls}))
        direct = SafeHttpClient(
            user_agent="GenchiCollector/0.1 (+https://genchi.news)",
            timeout_seconds=45,
            retries=3,
            max_response_bytes=15_000_000,
            obey_robots=True,
            allow_private_network=False,
            allowed_hosts=allowed_hosts,
            rate_limit_seconds=1,
        )
        browser = None
        if config.browser:
            browser = BrowserClient(config.browser_url, context.secret(config.browser_token_secret))

        def load(url: str, selector: str | None = None) -> tuple[str, str]:
            if browser:
                return browser.render(url, selector=selector)
            response = direct.get(url, allowed_content_types=("text/html", "application/xhtml+xml"))
            return response.text, response.url

        pending = list(config.start_urls)
        visited_pages: set[str] = set()
        detail_urls: list[str] = []
        while pending and len(visited_pages) < max_pages and len(detail_urls) < config.max_items:
            page_url = pending.pop(0)
            if page_url in visited_pages:
                continue
            html, final_url = load(page_url, config.wait_selector)
            visited_pages.add(page_url)
            soup = BeautifulSoup(html, "lxml")
            roots = soup.select(config.list_item_selector) if config.list_item_selector else [soup]
            for root in roots:
                for link in root.select(config.detail_link_selector):
                    raw = link.get("href")
                    detail_url = urljoin(final_url, str(raw)) if raw else ""
                    if detail_url and pattern.search(detail_url) and detail_url not in detail_urls:
                        detail_urls.append(detail_url)
                        if len(detail_urls) >= config.max_items:
                            break
            if config.next_selector:
                next_link = soup.select_one(config.next_selector)
                raw_next = next_link.get("href") if next_link else None
                next_url = urljoin(final_url, str(raw_next)) if raw_next else None
                if next_url and next_url not in visited_pages:
                    pending.append(next_url)

        if not detail_urls:
            raise TransientError("official site yielded no matching detail links")
        for detail_url in detail_urls:
            html, final_url = load(detail_url)
            soup = BeautifulSoup(html, "lxml")
            title = _meta(soup, "og:title", "twitter:title") or _text(soup, config.title_selector)
            content = _text(soup, config.content_selector) or _text(soup, "main")
            if not content:
                raise ConfigurationError(f"content selector did not match {final_url}")
            published_raw = _meta(soup, "article:published_time", "date", "pubdate") or _text(
                soup, config.published_selector
            )
            published_at = _time(published_raw)
            if request.window_start and published_at and published_at < request.window_start:
                continue
            if request.window_end and published_at and published_at >= request.window_end:
                continue
            media = []
            cover = _meta(soup, "og:image", "twitter:image")
            if cover:
                media.append({"type": "image", "url": urljoin(final_url, cover)})
            external_id = hashlib.sha256(final_url.encode()).hexdigest()
            context.emit(
                ResourceRecord(
                    external_id=f"web:{external_id}",
                    kind="official_news",
                    url=final_url,
                    title=title,
                    content=content,
                    content_type="text/plain",
                    language="ja",
                    published_at=published_at,
                    observed_at=datetime.now(UTC),
                    attributes={"media": media, "source_type": "official_site"},
                    tags=request.tags,
                )
            )
        return FetchReport(
            details={"list_pages": len(visited_pages), "detail_urls": len(detail_urls)}
        )

    def fetch(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = OfficialSiteConfig.model_validate(request.config)
        return self._run(context, request, max_pages=config.max_pages)

    def backfill(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = OfficialSiteConfig.model_validate(request.config)
        return self._run(context, request, max_pages=config.backfill_max_pages)
