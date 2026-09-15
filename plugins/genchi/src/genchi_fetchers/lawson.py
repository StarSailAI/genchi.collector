"""Lawson's public detail JSON, preserving native sessions and reception scopes."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit

from bs4 import BeautifulSoup


def detail_url(code: str, method: str | None = None, schedule: str | None = None) -> str:
    if not re.fullmatch(r"\d{5}", code):
        raise ValueError("Invalid Lawson L-code")
    query = {"gLcode": code}
    if method and schedule:
        if not method.isdigit() or not schedule.isdigit():
            raise ValueError("Invalid Lawson reception identity")
        query.update(gEntryMthd=method, gScheduleNo=schedule)
    return "https://l-tike.com/order/?" + urlencode(query)


def timestamp(value: str | None) -> str | None:
    if not value or str(value).startswith("9999"):
        return None
    value = str(value).strip()
    if re.fullmatch(r"\d{8} \d{4}", value):
        value = datetime.strptime(value, "%Y%m%d %H%M").isoformat() + "+09:00"
    parsed = datetime.fromisoformat(value)
    if not parsed.tzinfo:
        raise ValueError("Lawson timestamp lacks a timezone")
    return value


def parse_detail(html: str, url: str) -> dict:
    node = BeautifulSoup(html, "lxml").select_one("script#form-data[type='application/json']")
    if not node:
        raise ValueError("Lawson detail has no native form-data; not a complete schedule")
    data = json.loads(node.get_text())
    code = str(data.get("lCode") or "")
    if data.get("result") != 0 or not code or not data.get("evName"):
        raise ValueError("Lawson detail reports an error or lacks event identity")
    requested = parse_qs(urlsplit(url).query).get("gLcode", [])
    if requested != [code]:
        raise ValueError("Lawson detail identity differs from requested L-code")
    method, schedule = str(data.get("selectEntryMthd") or ""), str(data.get("selectSalesScheduleNo") or "")
    rounds = data.get("salesSystemBeanList") or []
    selected = next((r for r in rounds if str(r.get("entryMethod")) == method
                     and str(r.get("scheduleNo")) == schedule), None)
    rows = (data.get("pfInfoListBean") or {}).get("pfInfoDetailListBeanList") or []
    if not selected or not rows:
        raise ValueError("Lawson detail has no selected reception or native sessions")
    native_round = f"{code}:{method}:{schedule}"
    round_id = hashlib.sha256(f"lawson:reception:{native_round}".encode()).hexdigest()[:20]
    events = []
    for row in rows:
        raw_date = str(row.get("pfDateYYYYMMDD") or "")
        end_day = None
        if str(row.get("pfIdCls")) == "2":
            dates = re.findall(r"(20\d{2})/(\d{1,2})/(\d{1,2})", str(row.get("pfDate") or ""))
            if len(dates) != 2:
                raise ValueError("Lawson admission period lacks both date boundaries")
            day, end_day = [datetime(*map(int, d)).date().isoformat() for d in dates]
        elif re.fullmatch(r"20\d{6}", raw_date):
            day = datetime.strptime(raw_date, "%Y%m%d").date().isoformat()
        else:
            raise ValueError("Lawson native row has no supported exact date")
        for slot in row.get("pfInfoDetailList") or []:
            key = str(slot.get("pfKey") or "")
            if not key:
                raise ValueError("Lawson native session has no stable key")
            clock = str(slot.get("curtainTime") or "")
            doors = str(slot.get("openGateTime") or "")
            # Do not derive hours from the key or substitute midnight for a missing time.
            start = f"{day}T{clock}:00+09:00" if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock) else day
            label = selected.get("receptionName") or selected.get("saleMethodName") or "チケット受付"
            closes = timestamp(slot.get("salesEndDateTime")) or timestamp(slot.get("pfSaleEndDttm")) or timestamp(selected.get("reservationEndDateTime"))
            window = {
                "id": hashlib.sha256(f"lawson:reception:{native_round}:{key}".encode()).hexdigest()[:20],
                "roundId": round_id, "nativeReceptionKey": native_round,
                "nativePerformanceKeys": [key], "label": label,
                "phase": "RESALE" if "リセール" in label else "LOTTERY_1" if selected.get("entryName") == "抽選" else "GENERAL",
                "opensAt": timestamp(selected.get("reservationStartDateTime")) or timestamp(selected.get("receptionDateStart")),
                "closesAt": closes,
                "resultAt": timestamp(selected.get("winLoseAnnouceDate")),
                "url": detail_url(code, method, schedule),
                # Sold out / reception closed does not cancel the event or ticket round.
                "status": "CANCELED" if slot.get("entryStatusName") == "公演中止" else "CLOSED" if slot.get("entryStatusName") in {"予定枚数終了", "受付終了"} else None,
                "notes": selected.get("scheduleComment") or None,
            }
            events.append({
                "id": key, "nativePerformanceKey": key, "name": data["evName"],
                "activityKey": "native:lawson:lcode:" + code + ":" + day[:4],
                "nativeTitle": row.get("pfTitle"),
                "url": url, "startsAt": start, "endsAt": end_day,
                "doorsAt": f"{day}T{doors}:00+09:00" if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", doors) else None,
                "startLabel": slot.get("curtainCharDisp") or slot.get("curtainChar") or None,
                "nativeDateText": row.get("pfDate"), "nativeDateClass": row.get("pfIdCls"),
                "venue": {"name": row.get("venueName"), "prefecture": row.get("prefName"), "country": "日本"},
                "ticketWindows": [window],
            })
    if not events:
        raise ValueError("Lawson detail has no native sessions")
    return {
        "pageId": code, "title": data["evName"], "events": events,
        "selectedReception": native_round,
        "roundUrls": [detail_url(code, str(r["entryMethod"]), str(r["scheduleNo"])) for r in rounds],
        "scheduleCompleteness": "native_detail",
    }


def merge_details(pages: list[dict]) -> list[dict]:
    events = {}
    for page in pages:
        for row in page["events"]:
            current = events.get(row["id"])
            if current is None:
                events[row["id"]] = {**row, "ticketWindows": list(row["ticketWindows"])}
                continue
            if any(current.get(k) != row.get(k) for k in ("startsAt", "endsAt", "venue", "startLabel")):
                raise ValueError("Conflicting native Lawson session facts across receptions")
            known = {w["id"] for w in current["ticketWindows"]}
            current["ticketWindows"].extend(w for w in row["ticketWindows"] if w["id"] not in known)
    return list(events.values())
