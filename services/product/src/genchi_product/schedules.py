"""Shared schedule semantics for new writes and audited historical repairs."""
from __future__ import annotations

import re
import unicodedata

from .domain import JST, Moment, classify


def role_for(title: str, kind: str, source_label: str = "") -> str:
    text = unicodedata.normalize("NFKC", title)
    kind = classify(title, kind)
    if re.search(r"(?:ホテル|hotel).*?(?:ROOM|ルーム|宿泊)|宿泊プラン|チェックイン", text, re.I):
        return "STAY"
    if re.search(r"ライブビューイング|live.?viewing|映画|上映|FILM LIVE", text, re.I):
        return "SCREENING"
    if kind in {"EXHIBITION", "CAFE", "POPUP"} or re.search(r"入場|入館|入園|入店|開場", source_label):
        return "ADMISSION"
    if source_label == "開演" or kind in {"LIVE", "FESTIVAL", "MEETUP"}:
        return "PERFORMANCE"
    return "EVENT"


def occurrence_label(time: Moment, venue: str | None, label: str | None = None) -> str:
    if label:
        return label[:500]
    stamp = time.starts_at.astimezone(JST).strftime("%m/%d %H:%M") if time.starts_at else str(time.starts_on or "日期待公布")
    if time.ends_on and time.ends_on != time.starts_on:
        stamp += "—" + str(time.ends_on)
    return " · ".join(v for v in (stamp, venue) if v)[:500]


def schedule_node(title: str, kind: str, time: Moment, venue: str | None,
                  label: str | None = None, role: str | None = None) -> tuple[str, str, str]:
    role = role or role_for(title, kind)
    scope = occurrence_label(time, venue, label)
    if role == "ADMISSION":
        if time.ends_on and time.ends_on != time.starts_on:
            return "PERIOD", f"{scope} · 可入场期间"[:500], role
        return "DOORS", (f"{scope} · " + ("指定入场" if time.precision == "TIME" else "可入场日期（时刻待核验）"))[:500], role
    action = {"STAY": "办理入住", "SCREENING": "放映开始", "PERFORMANCE": "开演", "EVENT": "活动安排"}[role]
    if time.precision != "TIME":
        action = {"STAY": "可入住日期", "SCREENING": "放映日期", "PERFORMANCE": "演出日期", "EVENT": "举办日期"}[role] + "（时刻待核验）"
    return "START", f"{scope} · {action}"[:500], role
