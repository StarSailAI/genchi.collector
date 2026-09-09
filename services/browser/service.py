from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web
from camoufox.addons import DefaultAddons
from camoufox.async_api import AsyncCamoufox
from egress import BlockedDestination, EgressProxy, PublicResolver, public_url

LOGGER = logging.getLogger("genchi.browser")
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
CHALLENGE_MARKERS = (
    "verify you are human",
    "unusual traffic",
    "access denied",
    "cf-chl-",
)


def _enabled(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "no", "off", ""}


def _authorized(request: web.Request) -> bool:
    expected = os.environ.get("BROWSER_API_TOKEN", "").strip()
    return bool(expected) and secrets.compare_digest(
        request.headers.get("Authorization", ""), f"Bearer {expected}"
    )


def _json_error(status: int, code: str, detail: str) -> web.HTTPException:
    cls = {
        400: web.HTTPBadRequest,
        401: web.HTTPUnauthorized,
        409: web.HTTPConflict,
        502: web.HTTPBadGateway,
        503: web.HTTPServiceUnavailable,
    }[status]
    return cls(
        text=json.dumps({"code": code, "detail": detail}),
        content_type="application/json",
    )


RESOLVER = PublicResolver()


async def _validate_public_url(url: str) -> None:
    try:
        parsed = public_url(url)
        await RESOLVER.resolve(parsed.hostname)
    except (BlockedDestination, OSError, ValueError):
        raise _json_error(
            400, "PRIVATE_ADDRESS", "url must resolve to a public HTTP(S) address"
        ) from None


async def health(request: web.Request) -> web.Response:
    browser = request.app.get("browser")
    ready = browser is not None and browser.is_connected()
    return web.json_response(
        {
            "service": "genchi-browser",
            "ready": ready,
            "engine": "camoufox",
            "route": "F",
            "browserVersion": browser.version if ready else None,
            "build": os.environ.get("CAMOUFOX_BROWSER", "official/152.0.4-beta.30"),
        },
        status=200 if ready else 503,
    )


async def _new_page(request: web.Request, *, authenticated=False):
    browser = request.app.get("browser")
    if browser is None or not browser.is_connected():
        if not request.app.get("browser_manager"):
            raise _json_error(503, "NOT_READY", "browser is not ready")
        # A Firefox child can exit while the HTTP adapter stays alive. Recover
        # under a separate lock so concurrent requests never spawn two engines.
        async with request.app["browser_restart_lock"]:
            browser = request.app.get("browser")
            if browser is None or not browser.is_connected():
                LOGGER.warning("browser disconnected; restarting the F engine")
                await stop_browser(request.app)
                await start_browser(request.app)
            browser = request.app["browser"]
    context = (
        request.app["x_context"]
        if authenticated
        else await browser.new_context(
            accept_downloads=False,
            service_workers="block",
            ignore_https_errors=False,
        )
    )
    try:
        page = await context.new_page()
        page.on("dialog", lambda dialog: dialog.dismiss())
        await page.route_web_socket("**/*", lambda websocket: websocket.close())
        return page
    except BaseException:
        if not authenticated:
            await context.close()
        raise


async def _close_page(request: web.Request, page):
    if page.context is request.app["x_context"]:
        await page.close()
    else:
        await page.context.close()


async def _guard_public(route: Any, navigation_request: Any) -> bool:
    try:
        await _validate_public_url(navigation_request.url)
    except web.HTTPException:
        await route.abort("blockedbyclient")
        return False
    return True


async def fetch(request: web.Request) -> web.Response:
    if not _authorized(request):
        raise _json_error(401, "UNAUTHORIZED", "invalid browser API token")
    payload = await request.json()
    url = str(payload.get("url") or "").strip()
    await _validate_public_url(url)
    wait_seconds = max(0.0, min(30.0, float(payload.get("waitSeconds") or 2.0)))
    timeout_seconds = max(5.0, min(180.0, float(payload.get("timeoutSeconds") or 75.0)))
    selector = str(payload.get("selector") or "").strip()[:500]
    async with request.app["browser_semaphore"]:
        page = await _new_page(request)
        try:

            async def guard(route: Any, navigation_request: Any) -> None:
                if await _guard_public(route, navigation_request):
                    if _enabled("BROWSER_BLOCK_MEDIA", "1") and navigation_request.resource_type in {"image", "media", "font"}:
                        await route.abort("blockedbyclient")
                    else:
                        await route.continue_()

            await page.route("**/*", guard)
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=int(timeout_seconds * 1000)
            )
            if selector and (response is None or response.status < 400):
                # The requested content must exist. Navigation/header links are
                # not evidence that a client-rendered news list is ready.
                await page.wait_for_selector(selector, state="attached", timeout=15_000)
            if wait_seconds:
                await page.wait_for_timeout(int(wait_seconds * 1000))
            html = await page.content()
            final_url = page.url
            status = response.status if response else None
            retry_after = (getattr(response, "headers", {}) or {}).get("retry-after", "") if response else ""
        except Exception as exc:
            error_code = re.search(r"NS_ERROR_[A-Z_]+|net::ERR_[A-Z_]+", str(exc))
            LOGGER.warning("render failed host=%s error=%s code=%s", urlsplit(url).hostname, type(exc).__name__, error_code.group() if error_code else "unknown")
            raise _json_error(
                502, "FETCH_FAILED", f"browser fetch failed: {type(exc).__name__}"
            ) from exc
        finally:
            await _close_page(request, page)
    max_bytes = int(os.environ.get("BROWSER_MAX_HTML_BYTES", "12582912"))
    encoded = html.encode("utf-8")
    if len(encoded) > max_bytes:
        raise web.HTTPRequestEntityTooLarge(max_size=max_bytes, actual_size=len(encoded))
    return web.Response(
        body=encoded,
        content_type="text/html",
        charset="utf-8",
        headers={
            "X-Genchi-Browser-Upstream-Status": str(status or ""),
            "X-Genchi-Browser-Final-URL": final_url[:2000],
            "Cache-Control": "no-store",
            **({"Retry-After": retry_after[:100]} if retry_after and status in {429, 503} else {}),
        },
    )


EXTRACT_POSTS_JS = r"""
(articles) => articles.map((article) => {
  const attr = (selector, name) => {
    const node = article.querySelector(selector);
    return node ? (node.getAttribute(name) || node.textContent || '').trim() : null;
  };
  const text = (selector) => {
    const node = article.querySelector(selector);
    return node ? (node.innerText || node.textContent || '').trim() : null;
  };
  const permalink = Array.from(article.querySelectorAll('a[href*="/status/"]'))
    .map((node) => node.href).find((value) => /\/status\/\d+/.test(value));
  const idMatch = (article.dataset.tweetId || permalink || '').match(/(?:status\/)?(\d{6,})/);
  const media = [];
  for (const image of article.querySelectorAll('[data-testid="tweetPhoto"] img, video[poster]')) {
    const url = image.currentSrc || image.src || image.poster;
    if (url && !media.some((item) => item.url === url)) {
      media.push({type: image.tagName === 'VIDEO' ? 'video' : 'image', url});
    }
  }
  for (const image of article.querySelectorAll('meta[itemprop="image"], meta[itemprop="contentUrl"]')) {
    const url = image.getAttribute('content');
    if (url && !media.some((item) => item.url === url)) media.push({type: 'image', url});
  }
  const links = Array.from(article.querySelectorAll('a[href]'))
    .map((node) => node.href).filter((value, index, values) => value && values.indexOf(value) === index);
  const metric = (testId) => {
    const node = article.querySelector(`[data-testid="${testId}"]`);
    return node ? (node.getAttribute('aria-label') || node.innerText || '').trim() : null;
  };
  const articleBody = attr('[itemprop="articleBody"]', 'content') || text('[itemprop="articleBody"]') || text('[data-testid="tweetText"]');
  return {
    id: idMatch ? idMatch[1] : attr('[itemprop="identifier"]', 'content'),
    url: permalink || attr('[itemprop="url"]', 'href') || attr('[itemprop="url"]', 'content'),
    authorHandle: attr('[itemprop="author"] [itemprop="alternateName"]', 'content') || text('[data-testid="User-Name"] a[href^="/"] span'),
    authorName: attr('[itemprop="author"] [itemprop="name"]', 'content') || text('[data-testid="User-Name"]'),
    text: articleBody,
    publishedAt: attr('time', 'datetime') || attr('[itemprop="datePublished"]', 'content'),
    links,
    hashtags: (articleBody || '').match(/#[\p{L}\p{N}_]+/gu) || [],
    media,
    pinned: /(^|\n)(Pinned|固定済み)(\n|$)/i.test(article.innerText || ''),
    metrics: {reply: metric('reply'), repost: metric('retweet'), like: metric('like')}
  };
}).filter((post) => post.id && post.url && post.text)
"""


async def extract_x_profile(request: web.Request) -> web.Response:
    if not _authorized(request):
        raise _json_error(401, "UNAUTHORIZED", "invalid browser API token")
    payload = await request.json()
    handle = str(payload.get("handle") or "").lstrip("@").strip()
    if not HANDLE_RE.fullmatch(handle):
        raise _json_error(400, "INVALID_HANDLE", "handle must be a valid X account name")
    max_posts = max(1, min(200, int(payload.get("maxPosts") or 50)))
    max_scrolls = max(0, min(30, int(payload.get("maxScrolls") or 8)))
    known_ids = {str(value) for value in payload.get("knownPostIds") or []}
    async with request.app["browser_semaphore"]:
        page = await _new_page(request, authenticated=True)
        try:

            async def guard(route: Any, navigation_request: Any) -> None:
                if (
                    navigation_request.is_navigation_request()
                    and navigation_request.frame == page.main_frame
                ):
                    target = urlsplit(navigation_request.url)
                    if target.hostname not in X_HOSTS:
                        await route.abort("blockedbyclient")
                        return
                if await _guard_public(route, navigation_request):
                    await route.continue_()

            await page.route("**/*", guard)
            await page.goto(
                f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=90_000
            )
            await page.wait_for_timeout(3_000)
            posts: dict[str, dict[str, Any]] = {}
            no_growth = 0
            for _ in range(max_scrolls + 1):
                batch = await page.locator(
                    'article[data-tweet-id], article[itemtype*="SocialMediaPosting"], article[data-testid="tweet"]'
                ).evaluate_all(EXTRACT_POSTS_JS)
                before = len(posts)
                for post in batch:
                    posts[str(post["id"])] = post
                if len(posts) >= max_posts or (known_ids and known_ids.intersection(posts)):
                    break
                no_growth = no_growth + 1 if len(posts) == before else 0
                if no_growth >= 2:
                    break
                await page.mouse.wheel(0, 1800)
                await page.wait_for_timeout(1_500)
            body_text = (await page.locator("body").inner_text(timeout=5_000))[:10_000]
            final_url = page.url
        except Exception as exc:
            LOGGER.warning("X extraction failed handle=%s error=%s", handle, type(exc).__name__)
            raise _json_error(502, "EXTRACTION_FAILED", type(exc).__name__) from exc
        finally:
            await _close_page(request, page)
    lowered = body_text.lower()
    if not posts:
        marker = next((value for value in CHALLENGE_MARKERS if value in lowered), None)
        if marker:
            raise _json_error(409, "CHALLENGE", f"X challenge detected: {marker}")
        if "/login" in final_url or "log in to x" in lowered:
            raise _json_error(409, "LOGIN_REQUIRED", "X requires an authenticated profile")
        raise _json_error(409, "EMPTY_OR_MARKUP_CHANGED", "no semantic X posts were found")
    ordered = sorted(
        posts.values(), key=lambda value: value.get("publishedAt") or "", reverse=True
    )[:max_posts]
    return web.json_response(
        {
            "profile": {"handle": handle, "url": f"https://x.com/{handle}"},
            "posts": ordered,
            "observedAt": __import__("datetime")
            .datetime.now(__import__("datetime").UTC)
            .isoformat(),
            "extractorVersion": "1.0.0",
        },
        headers={"Cache-Control": "no-store"},
    )


async def start_browser(app: web.Application) -> None:
    proxy = EgressProxy(RESOLVER)
    await proxy.start()
    app["egress_proxy"] = proxy
    manager = AsyncCamoufox(
        browser=os.environ.get("CAMOUFOX_BROWSER", "official/152.0.4-beta.30"),
        headless=_enabled("BROWSER_HEADLESS", "1"),
        locale=os.environ.get("BROWSER_LOCALE", "ja-JP"),
        exclude_addons=[DefaultAddons.UBO],
        proxy={"server": proxy.url},
        block_webrtc=True,
        firefox_user_prefs={
            "network.proxy.no_proxies_on": "",
            "network.proxy.allow_hijacking_localhost": True,
            "network.http.http3.enable": False,
            "network.dns.disablePrefetch": True,
            "security.fileuri.strict_origin_policy": True,
        },
    )
    app["browser_manager"] = manager
    try:
        app["browser"] = await manager.__aenter__()
        app["x_context"] = await app["browser"].new_context(
            accept_downloads=False,
            service_workers="block",
            ignore_https_errors=False,
        )
        cookie_path = os.environ.get("BROWSER_X_COOKIES_PATH", "").strip()
        if cookie_path:
            cookies = json.loads(Path(cookie_path).read_text())
            await app["x_context"].add_cookies(cookies)
    except BaseException:
        await stop_browser(app)
        raise
    LOGGER.info("Camoufox ready route=F version=%s", app["browser"].version)


async def stop_browser(app: web.Application) -> None:
    try:
        if app.get("browser") is not None:
            await app["browser_manager"].__aexit__(None, None, None)
    finally:
        app["browser"] = None
        if app.get("egress_proxy"):
            await app["egress_proxy"].close()


def create_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    concurrency = max(1, min(8, int(os.environ.get("BROWSER_CONCURRENCY", "1"))))
    app["browser"] = None
    app["x_context"] = None
    app["browser_semaphore"] = asyncio.Semaphore(concurrency)
    app["browser_restart_lock"] = asyncio.Lock()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/fetch", fetch)
    app.router.add_post("/extract/x-profile", extract_x_profile)
    app.on_startup.append(start_browser)
    app.on_cleanup.append(stop_browser)
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    web.run_app(create_app(), host="0.0.0.0", port=3003, access_log=LOGGER)
