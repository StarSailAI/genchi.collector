"""Reviewed physical venue identities, never fuzzy building/hall merges."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .domain import normalize


def venue_key(venue: str | None, title: str = "", year: str = "") -> str:
    key = normalize(venue or "")
    # The arena's formal name and its ticket-site shorthand identify one hall.
    if key in {normalize("北海きたえーる"),
               normalize("北海道立総合体育センター 北海きたえーる")}:
        return "venue:hokkaido-kitaeru"
    # Official rename: https://www.neec.ac.jp/information/55354/
    if key in {
        normalize(x)
        for x in ("日本工学院アリーナ", "片柳アリーナ", "日本工学院アリーナ（片柳アリーナ）")
    }:
        return "venue:nihon-kogakuin-arena"
    # Event-scoped, not a global claim that every conference room is this hall.
    # Venue announcement: https://www.yaesu.theater-conference.jp/news/-xz40DlG
    # Ticket details: https://t.pia.jp/pia/event/event.do?eventBundleCd=b2669161
    if year == "2026" and "aqours" in normalize(title) and "diveintosparkle" in normalize(title):
        if key in {
            normalize(x)
            for x in (
                "東京建物ぴあカンファレンス",
                "東京建物ぴあカンファレンス TO YAESU HALL",
                "東京建物ぴあカンファレンス 6F TO YAESU HALL",
            )
        }:
            return "venue:to-yaesu-hall"
    return key


def nonphysical_venue(venue: str | None, url: str | None = None) -> bool:
    key = normalize(venue or "")
    if key in {"spwn", "pialivestream", "streaming", "配信", "オンライン", "online", "zaiko"}:
        return True
    parsed = urlsplit(url or "")
    return parsed.hostname == "eplus.jp" and parsed.path.rstrip("/") == "/sf/streamingplus"


def bundle_venue(venue: str | None) -> bool:
    return bool(re.search(r"通し(?:チケット|券)", venue or ""))
