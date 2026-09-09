from __future__ import annotations

from allfeeds_sdk import FetchContext, FetchRequest
from genchi_fetchers import (
    AsobiTicketFetcher,
    EplusTicketFetcher,
    LawsonTicketFetcher,
    OfficialSiteFetcher,
    PiaTicketFetcher,
    XProfileFetcher,
)
from genchi_fetchers.fetchers import (
    _eplus_ticket_phase,
    _pia_ticket_phase,
    _pia_title,
    _response_html,
)
from genchi_normalizer.app import _category, _eplus_event_type, _stable_id


def context(records, checkpoint=None):
    state = dict(checkpoint or {})
    return FetchContext(
        emit_record=records.append,
        emit_asset=lambda *_: None,
        load_checkpoint=lambda: state,
        save_checkpoint=lambda value: state.update(value),
        secret_provider=lambda name: "browser-token" if name == "BROWSER_API_TOKEN" else None,
        logger=None,
    )


def test_x_profile_emits_stable_post_ids(monkeypatch):
    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "observedAt": "2026-07-21T00:00:00Z",
                "extractorVersion": "1.0.0",
                "posts": [
                    {
                        "id": "1234567890",
                        "url": "https://x.com/example/status/1234567890",
                        "text": "ライブ開催決定 #テスト",
                        "publishedAt": "2026-07-20T12:00:00Z",
                        "media": [{"type": "image", "url": "https://pbs.twimg.com/a.jpg"}],
                    }
                ],
            }

    monkeypatch.setattr(
        "genchi_fetchers.fetchers.requests.post", lambda *args, **kwargs: Response()
    )
    records = []
    fetch_context = context(records)
    report = XProfileFetcher().fetch(
        fetch_context,
        FetchRequest(
            task_id=1,
            source_id="example-x",
            operation="fetch",
            config={"handle": "example"},
            tags=("project:example",),
        ),
    )
    assert report.details["posts"] == 1
    assert records[0].external_id == "x:1234567890"
    assert records[0].attributes["media"][0]["type"] == "image"
    assert fetch_context.checkpoint()["known_post_ids"] == ["1234567890"]


def test_official_site_discovers_and_parses_detail(monkeypatch):
    pages = {
        "https://example.com/news/": '<main><a href="/news/42/">detail</a></main>',
        "https://example.com/news/42/": """
            <article><h1>公演開催決定</h1><time>2026.07.21</time><p>本文です。</p></article>
            <meta property="og:image" content="/cover.jpg">
        """,
    }

    class Response:
        def __init__(self, url):
            self.url = url
            self.text = pages[url]

    monkeypatch.setattr(
        "genchi_fetchers.fetchers.SafeHttpClient.get",
        lambda self, url, **kwargs: Response(url),
    )
    records = []
    report = OfficialSiteFetcher().fetch(
        context(records),
        FetchRequest(
            task_id=2,
            source_id="example-news",
            operation="fetch",
            config={
                "start_urls": ["https://example.com/news/"],
                "link_pattern": r"^https://example\.com/news/[0-9]+/$",
                "title_selector": "h1",
                "content_selector": "article",
                "published_selector": "time",
            },
            tags=("project:example",),
        ),
    )
    assert report.details["detail_urls"] == 1
    assert records[0].title == "公演開催決定"
    assert records[0].published_at.isoformat() == "2026-07-21T00:00:00+09:00"
    assert records[0].attributes["published_precision"] == "DATE"
    assert records[0].attributes["published_on"] == "2026-07-21"
    assert records[0].attributes["media"][0]["url"] == "https://example.com/cover.jpg"


def test_rule_category_and_stable_ids():
    assert _category("チケット先行受付を開始") == "EVENT"
    assert _category("New Album Release") == "RELEASE"
    assert _stable_id("content", "a") == _stable_id("content", "a")


def test_asobi_ticket_emits_booth_reception_and_act(monkeypatch):
    responses = {
        "https://api.example.com/v1/public/booths?page=1&per_page=50": {
            "data": [
                {
                    "id": "idolmaster-260101",
                    "type": "booth",
                    "attributes": {
                        "slug": "idolmaster-260101",
                        "name": "THE IDOLM@STER TEST LIVE",
                    },
                }
            ]
        },
        "https://api.example.com/v1/public/booths/idolmaster-260101": {
            "data": {
                "id": "idolmaster-260101",
                "type": "booth",
                "attributes": {
                    "slug": "idolmaster-260101",
                    "name": "THE IDOLM@STER TEST LIVE",
                    "main_body": "<p>Booth body</p>",
                },
            },
            "included": [],
        },
        "https://api.example.com/v1/public/receptions?booth_slug=idolmaster-260101": {
            "data": [
                {
                    "id": "reception-1",
                    "type": "reception",
                    "attributes": {
                        "name": "一般発売",
                        "entry_type": "fcfs",
                        "entry_period_starts_at": "2026-01-01T12:00:00+09:00",
                        "entry_period_ends_at": "2026-01-02T23:59:59+09:00",
                        "top_body": "<p>Reception body</p>",
                    },
                    "relationships": {"resale_act": {"data": None}},
                }
            ],
            "included": [
                {
                    "id": "tour-1",
                    "type": "tour",
                    "attributes": {"name": "THE IDOLM@STER TEST LIVE"},
                },
                {
                    "id": "act-1",
                    "type": "act",
                    "attributes": {
                        "name": "DAY 1",
                        "venue": "Test Hall",
                        "performance_date": "2026-02-01",
                        "opens_at": "2026-02-01T16:00:00+09:00",
                        "performance_starts_at": "2026-02-01T17:00:00+09:00",
                    },
                },
            ],
        },
    }

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    monkeypatch.setattr(
        "genchi_fetchers.fetchers.SafeHttpClient.get",
        lambda _self, url, **_kwargs: Response(responses[url]),
    )
    records = []
    fetch_context = context(records)
    report = AsobiTicketFetcher().fetch(
        fetch_context,
        FetchRequest(
            task_id=3,
            source_id="asobi-ticket-booths",
            operation="fetch",
            config={
                "api_base_url": "https://api.example.com/v1/public",
                "site_base_url": "https://tickets.example.com",
            },
            tags=("project:idolmaster", "country:JP"),
        ),
    )

    assert report.details == {"pages": 1, "booths": 1, "receptions": 1, "acts": 1}
    assert {record.kind for record in records} == {
        "ticket_booth",
        "ticket_reception",
        "ticket_act",
    }
    reception = next(record for record in records if record.kind == "ticket_reception")
    assert reception.external_id == "asobi:reception:reception-1"
    assert "受付開始: 2026-01-01T12:00:00+09:00" in reception.content
    assert reception.attributes["asobi_ticket"]["acts"][0]["id"] == "act-1"


def test_eplus_ticket_discovers_parses_and_filters_anime_page(monkeypatch):
    category_url = "https://eplus.jp/sf/anime/kanto"
    detail_url = "https://eplus.jp/sf/detail/4512340001"
    pages = {
        category_url: """
            <html><head></head><body>
              <a href="/sf/detail/4512340001-P0030001P021001">公演</a>
            </body></html>
        """,
        detail_url: """
            <html><head>
              <meta property="og:image" content="https://cdn.example.com/imas.jpg">
              <script type="application/ld+json">
                {
                  "@context":"https://schema.org",
                  "@type":"Event",
                  "url":"https://eplus.jp/sf/detail/4512340001-P0030001P021001",
                  "name":"THE IDOLM@STER TEST LIVE &lt;DAY1&gt;",
                  "startDate":"2026-09-01",
                  "endDate":"2026-09-01T20:00",
                  "location":{
                    "@type":"Place",
                    "name":"テストホール",
                    "url":"https://eplus.jp/sf/venue/1234560",
                    "address":{
                      "@type":"PostalAddress",
                      "addressRegion":"東京都",
                      "addressCountry":"日本"
                    }
                  }
                }
              </script>
            </head><body>
              <nav class="section--s4-breadcrumbs">
                <span class="breadcrumb-list__name">アニメ・ゲーム</span>
              </nav>
              <article class="block-ticket-article">
                <p>2026/9/1(火) 開演：17:00～ (開場 16:00～)</p>
                <section class="block-ticket">
                  <h4 class="block-ticket__title">抽選 ★プレオーダー</h4>
                  <p class="block-ticket__time">
                    受付期間:2026/7/23(木)12:00～2026/7/30(木)18:00
                  </p>
                  <span>受付中</span>
                </section>
              </article>
            </body></html>
        """,
    }

    class Response:
        def __init__(self, url):
            self.url = url
            self.text = pages[url]

    monkeypatch.setattr(
        "genchi_fetchers.fetchers.SafeHttpClient.get",
        lambda _self, url, **_kwargs: Response(url),
    )
    records = []
    fetch_context = context(records)
    report = EplusTicketFetcher().fetch(
        fetch_context,
        FetchRequest(
            task_id=4,
            source_id="eplus-anime-tickets",
            operation="fetch",
            config={
                "category_urls": [category_url],
                "roots_per_run": 1,
                "pages_per_root": 1,
                "refresh_details_per_run": 0,
                "rate_limit_seconds": 1,
                "browser_fallback": False,
            },
            tags=("country:JP", "timezone:Asia/Tokyo"),
        ),
    )

    assert report.details["details"] == 1
    assert report.details["events"] == 1
    assert report.details["ticket_windows"] == 1
    assert records[0].external_id == "eplus:detail:4512340001"
    assert records[0].title == "THE IDOLM@STER TEST LIVE <DAY1>"
    assert records[0].tags[-1] == "project:idolmaster"
    payload = records[0].attributes["eplus_ticket"]
    assert payload["nativeCategories"] == ["アニメ・ゲーム"]
    assert payload["discovery"] == [
        {
            "kind": "platform_category",
            "sourceUrl": category_url,
            "trustedCategory": True,
        }
    ]
    event = payload["events"][0]
    assert event["startsAt"] == "2026-09-01T17:00:00+09:00"
    assert event["doorsAt"] == "2026-09-01T16:00:00+09:00"
    assert event["venue"]["name"] == "テストホール"
    assert event["ticketWindows"][0] == {
        "id": event["ticketWindows"][0]["id"],
        "label": "抽選 ★プレオーダー",
        "phase": "LOTTERY_1",
        "opensAt": "2026-07-23T12:00:00+09:00",
        "closesAt": "2026-07-30T18:00:00+09:00",
        "status": "OPEN",
    }
    assert fetch_context.checkpoint()["tracked_detail_urls"] == [detail_url]


def test_eplus_ticket_phase_and_event_type_rules():
    assert _eplus_ticket_phase("FC会員先行") == "FC_PRE"
    assert _eplus_ticket_phase("抽選 2次プレオーダー") == "LOTTERY_2"
    assert _eplus_ticket_phase("先着 ★一般発売") == "GENERAL"
    assert _eplus_ticket_phase("公式リセール") == "RESALE"
    assert _eplus_event_type("アフタヌーン40周年展") == "OTHER"
    assert _eplus_event_type("GAME MUSIC FESTIVAL") == "FES"
    assert _eplus_event_type("声優 SPECIAL LIVE") == "LIVE"


def test_pia_response_uses_declared_utf8_instead_of_lxml_encoding_guess():
    class Response:
        encoding = "UTF-8"
        content = (
            '<html><head><meta property="og:title" '
            'content="「ヒックとドラゴン2」in コンサート ＜アニメ版2作目＞ | チケットぴあ">'
            "</head></html>"
        ).encode()

    assert _pia_title(_response_html(Response())) == (
        "「ヒックとドラゴン2」in コンサート ＜アニメ版2作目＞"
    )


def test_pia_ticket_phase_does_not_use_resale_navigation_when_label_is_specific():
    page_text = "チケットぴあ リセール お知らせ 一般発売"

    assert _pia_ticket_phase("先行先着", page_text) == "ADVANCE"
    assert _pia_ticket_phase("一般発売", page_text) == "GENERAL"
    assert _pia_ticket_phase("", "公式リセール 受付中") == "RESALE"


def test_pia_ticket_discovers_sales_and_exact_performances(monkeypatch):
    discovery_url = "https://t.pia.jp/pia/tag/tag.do?tagCd=0000037"
    detail_url = "https://t.pia.jp/pia/event/event.do?eventBundleCd=b2600001"
    sale_url = "https://t.pia.jp/pia/ticketInformation.do?eventCd=2600001&rlsCd=001"
    pages = {
        discovery_url: f'<a href="{detail_url}">アイドルマスター</a>',
        detail_url: f"""
            <html><head>
              <meta property="og:title" content="THE IDOLM@STER TEST LIVE">
              <meta property="og:image" content="https://image.pia.jp/test.jpg">
            </head><body>
              <div class="ticketSalesCard-2024">
                <a href="{sale_url}">
                  <p class="ticketSalesCard-2024__title">一般発売</p>
                  <p class="ticketSalesCard-2024__status">販売期間中</p>
                </a>
              </div>
            </body></html>
        """,
        sale_url: """
            <html><body>
              <div class="textLabel">
                <span class="textLabel--title">一般発売</span>
                <span>販売期間中 ～2026/8/30(日) 23:59</span>
              </div>
              <dl class="dataList">
                <dt>発売開始</dt><dd>2026/7/23(木) 昼12:00～</dd>
              </dl>
              <div class="Y15-regular-section">
                <dt class="Y15-event-date">2026/9/1(火)</dt>
                <dd class="Y15-event-time">17:00 開演 ( 16:00 開場 )</dd>
                <p class="Y15-event-site-place">会場：テストホール (東京都)</p>
                <input class="eventCd" value="2600001">
                <input class="perfCd" value="001">
              </div>
            </body></html>
        """,
    }

    class Response:
        def __init__(self, url):
            self.url = url
            self.content = pages[url].encode()

    monkeypatch.setattr(
        "genchi_fetchers.fetchers.SafeHttpClient.get",
        lambda _self, url, **_kwargs: Response(url),
    )
    records = []
    report = PiaTicketFetcher().fetch(
        context(records),
        FetchRequest(
            task_id=5,
            source_id="pia-anime-tickets",
            operation="fetch",
            config={
                "discovery_urls": [discovery_url],
                "search_keywords": ["アイドルマスター"],
                "keywords_per_run": 0,
                "refresh_details_per_run": 0,
                "max_detail_pages": 5,
                "max_sales_per_detail": 5,
                "rate_limit_seconds": 1,
                "browser_fallback": False,
            },
            tags=("country:JP", "timezone:Asia/Tokyo"),
        ),
    )

    assert report.details["details"] == 1
    assert report.details["sale_pages"] == 1
    assert records[0].external_id == "pia:detail:b2600001"
    assert records[0].tags[-1] == "project:idolmaster"
    payload = records[0].attributes["ticket_page"]
    assert payload["discovery"] == [
        {
            "kind": "platform_category",
            "sourceUrl": discovery_url,
            "trustedCategory": True,
        }
    ]
    event = payload["events"][0]
    assert event["id"] == "2600001-001"
    assert event["startsAt"] == "2026-09-01T17:00:00+09:00"
    assert event["doorsAt"] == "2026-09-01T16:00:00+09:00"
    assert event["venue"]["name"] == "テストホール"
    assert event["ticketWindows"][0]["phase"] == "GENERAL"
    assert event["ticketWindows"][0]["opensAt"] == "2026-07-23T12:00:00+09:00"
    assert event["ticketWindows"][0]["closesAt"] == "2026-08-30T23:59:00+09:00"


def test_lawson_ticket_uses_browser_and_deduplicates_same_day(monkeypatch):
    page = """
        <html><body><div id="layout_search_result">
          <div class="ResultBox">
            <span class="ResultBox__type">コンサート アニメ・ゲーム</span>
            <h3 class="ResultBox__title">THE IDOLM@STER TEST LIVE</h3>
            <div class="ResultBox__information">
              <dt class="ResultBox__informationTitle">公演日：</dt>
              <dt class="ResultBox__informationText">2026/9/1(火)</dt>
            </div>
            <div class="ResultBox__information">
              <dt class="ResultBox__informationTitle">会場：</dt>
              <dt class="ResultBox__informationText">テストホール（東京都）</dt>
            </div>
            <div class="ResultBox__table prfItem">
              <span id="reception_typename">抽選</span>
              <span id="sale_name">プレリク先行</span>
              <p id="receiptDat">2026/7/23(木) 12:00 ～ 2026/7/30(木) 18:00</p>
              <p class="orderEnd">受付中</p>
              <a class="entryBtn" data-lcode="12345"
                 data-basevenuename="テストホール（東京都）"
                 data-prfdate="20260901,20260901">詳細</a>
            </div>
          </div>
        </div></body></html>
    """
    monkeypatch.setattr(
        "genchi_fetchers.fetchers.BrowserClient.render",
        lambda _self, url, **_kwargs: (page, url),
    )
    records = []
    report = LawsonTicketFetcher().fetch(
        context(records),
        FetchRequest(
            task_id=6,
            source_id="lawson-anime-tickets",
            operation="fetch",
            config={
                "search_keywords": ["アイドルマスター"],
                "queries_per_run": 1,
                "max_results_per_run": 10,
            },
            tags=("country:JP", "timezone:Asia/Tokyo"),
        ),
    )

    assert report.details["results"] == 1
    assert report.details["events"] == 1
    assert records[0].external_id == "lawson:result:12345"
    payload = records[0].attributes["ticket_page"]
    assert payload["nativeCategories"] == ["コンサート アニメ・ゲーム"]
    assert payload["discovery"][0]["searchQuery"] == "アイドルマスター"
    assert payload["discovery"][0]["trustedCategory"] is False
    event = payload["events"][0]
    assert event["startsAt"] == "2026-09-01"
    assert event["ticketWindows"][0]["phase"] == "LOTTERY_1"
    assert event["ticketWindows"][0]["status"] == "OPEN"
    assert event["ticketWindows"][0]["opensAt"] == "2026-07-23T12:00:00+09:00"
