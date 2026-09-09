"""Parse public X DOM without account state, generated CSS classes or relative dates."""
from __future__ import annotations

import copy
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

POST_PATH = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d{6,25})/?$")
X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
BODY_SELECTOR = '[data-testid="tweetText"], [itemprop="articleBody"], div[dir="auto"]'
EXPAND_TEXT = {"さらに表示", "もっと見る", "Show more", "Read more"}


def post_identity(url):
    parsed = urlsplit(urljoin("https://x.com", url or ""))
    match = POST_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.hostname not in X_HOSTS or not match or match[1] == "i":
        return None
    return {"id": match[2], "authorHandle": match[1], "url": f"https://x.com/{match[1]}/status/{match[2]}"}


def _meta(soup, name):
    node = soup.select_one(f'meta[property="{name}"], meta[name="{name}"]')
    return node.get("content", "").strip() if node else ""


def _own_nodes(article, selector):
    return [n for n in article.select(selector) if n.find_parent("article") is article]


def _body(article):
    nodes = _own_nodes(article, BODY_SELECTOR)
    # The first body belongs to the outer post; quoted posts and replies must
    # never be concatenated into its evidence text.
    return nodes[0] if nodes else None


def _text(node):
    if node is None:
        return ""
    node = copy.deepcopy(node)
    for button in node.select("button"):
        button.decompose()
    for br in node.select("br"):
        br.replace_with("\n")
    for img in node.select("img[alt]"):
        img.replace_with(img["alt"])
    return (node.get("content") or node.get_text()).strip()


def _profile_links(article, handle):
    return [a for a in _own_nodes(article, "a[href]")
            if urlsplit(urljoin("https://x.com", a["href"])).path.rstrip("/").lower() == f"/{handle.lower()}"]


def parse_profile(html):
    soup = BeautifulSoup(html, "html.parser")
    posts, malformed = {}, 0
    for article in soup.select("article"):
        if article.find_parent("article"):
            continue
        identity = next((identity for a in _own_nodes(article, 'a[href*="/status/"]')
                         if (identity := post_identity(a["href"]))), None)
        if not identity:
            continue
        body = _body(article)
        if body is None and not article.select('a[href*="/photo/"], a[href*="/video/"], [data-testid="tweetPhoto"]'):
            malformed += 1
            continue
        social = article.select_one('[data-testid="socialContext"]')
        pinned = bool(article.select_one('[data-icon*="pin"]')) or bool(
            social and re.search(r"Pinned|固定", social.get_text(), re.I))
        # Pin banners in the public DOM may immediately precede the article.
        previous = article.find_previous_sibling()
        if previous and previous.name != "article" and previous.get_text(strip=True) in {"固定", "固定済み", "Pinned"}:
            pinned = True
        posts[identity["id"]] = {**identity, "pinned": pinned}
    return list(posts.values()), malformed


def parse_detail(html, expected_url):
    expected = post_identity(expected_url)
    if not expected:
        raise ValueError("invalid post permalink")
    soup = BeautifulSoup(html, "html.parser")
    canonical = _meta(soup, "og:url")
    canonical_node = soup.select_one('link[rel="canonical"]')
    if not canonical and canonical_node:
        canonical = canonical_node.get("href", "")
    identity = post_identity(canonical)
    if not identity or identity["id"] != expected["id"] or identity["authorHandle"].lower() != expected["authorHandle"].lower():
        raise ValueError("detail canonical URL does not match the requested post")
    roots = [a for a in soup.select("article") if not a.find_parent("article")]
    article = next((a for a in roots if _profile_links(a, identity["authorHandle"]) and any(
        (p := post_identity(n["href"])) and p["id"] == identity["id"]
        for n in _own_nodes(a, 'a[href*="/status/"]'))), None)
    if article is None:
        raise ValueError("requested post article is missing")
    body = _body(article)
    if body and any(n.get_text(strip=True) in EXPAND_TEXT for n in body.select("button, a")):
        raise ValueError("post body is still collapsed")
    published = _meta(soup, "article:published_time")
    if not published:
        stamp = article.select_one('time[datetime], [itemprop="datePublished"]')
        published = (stamp.get("datetime") or stamp.get("content") or "") if stamp else ""
    try:
        date = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("post has no absolute publication timestamp") from None
    if date.tzinfo is None:
        raise ValueError("post timestamp has no timezone")
    text = _text(body)
    media = []
    image = _meta(soup, "og:image")
    if image and urlsplit(image).hostname == "pbs.twimg.com":
        media.append({"type": "image", "url": image})
    for a in _own_nodes(article, 'a[href*="/photo/"], a[href*="/video/"]'):
        url = urljoin("https://x.com", a["href"])
        parsed = urlsplit(url)
        if parsed.hostname in X_HOSTS and re.fullmatch(rf"/{re.escape(identity['authorHandle'])}/status/{identity['id']}/(?:photo|video)/\d+", parsed.path, re.I):
            item = {"type": "video" if "/video/" in parsed.path else "image", "url": url}
            if item not in media:
                media.append(item)
    if not text and not media:
        raise ValueError("post has neither body text nor media")
    links = list(dict.fromkeys(urljoin("https://x.com", a["href"]) for a in body.select("a[href]"))) if body else []
    links = [u for u in links if urlsplit(u).scheme in {"http", "https"}]
    names = _profile_links(article, identity["authorHandle"])
    name = next((a.get_text(strip=True) for a in names if a.get_text(strip=True) and not a.get_text(strip=True).startswith("@")), None)
    quoted = list(dict.fromkeys(p["url"] for a in article.select('a[href*="/status/"]')
                               if (p := post_identity(a["href"])) and p["id"] != identity["id"]))
    return {**identity, "authorName": name, "text": text, "publishedAt": date.isoformat(),
            "links": links, "hashtags": re.findall(r"#[\w\u3040-\u30ff\u3400-\u9fff]+", text),
            "media": media, "quotedPostUrls": quoted, "textComplete": True,
            "publicationTimeSource": "page_metadata" if _meta(soup, "article:published_time") else "page_datetime"}


def failure_code(html, final_url):
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style"):
        node.decompose()
    body = soup.get_text(" ", strip=True).lower()
    if any(m in body for m in ("verify you are human", "unusual traffic", "access denied")):
        return "CHALLENGE"
    if "/login" in final_url or "/i/flow/" in final_url or any(m in body for m in ("log in to x", "xにログイン")):
        return "LOGIN_REQUIRED"
    return "EMPTY_OR_MARKUP_CHANGED"
