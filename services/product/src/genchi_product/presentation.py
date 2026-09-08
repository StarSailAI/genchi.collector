"""Read models for personal agendas; windows remain windows, never synthetic dates."""

from __future__ import annotations

from datetime import date, datetime

from .domain import JST
from .naming import title


def followed_ids(conn, account_id: str) -> list[str]:
    # Resolve subjects once for the account, including optional child subjects.
    # Unsubscribing email does not erase the user's personal agenda.
    return [
        r["id"]
        for r in conn.execute(
            """WITH RECURSIVE topics AS (
          SELECT f.id AS follow_id,f.target_id AS slug,f.include_children
          FROM genchi_private.follows f WHERE f.account_id=%s AND f.target_type='SUBJECT'
          UNION SELECT t.follow_id,s.slug,t.include_children FROM topics t
          JOIN catalog_subjects s ON s.parent_slug=t.slug WHERE t.include_children
        ) SELECT a.id FROM catalog_activities a
        WHERE a.publication='PUBLISHED' AND a.attendance IN ('OFFLINE','HYBRID')
          AND EXISTS(SELECT 1 FROM genchi_private.follows f WHERE f.account_id=%s
            AND ((f.target_type='ACTIVITY' AND f.target_id=a.id) OR
              (f.target_type='SUBJECT' AND EXISTS(SELECT 1 FROM catalog_activity_subjects s
                JOIN topics t ON t.slug=s.subject_slug AND t.follow_id=f.id WHERE s.activity_id=a.id)))
            AND (jsonb_array_length(f.kinds)=0 OR f.kinds ? a.kind)
            AND (jsonb_array_length(f.cities)=0 OR EXISTS(SELECT 1 FROM catalog_occurrences o
              WHERE o.activity_id=a.id AND f.cities ? o.city)))""",
            (account_id, account_id),
        ).fetchall()
    ]


def boundaries(node: dict) -> list[dict]:
    """Project actual starts/ends, retaining DATE precision and shared-day boundaries."""
    if node["precision"] == "TBD":
        return []
    precise = node["precision"] == "TIME"
    start = node["starts_at"] if precise else node["starts_on"]
    end = node["ends_at"] if precise else node["ends_on"]
    kind = node["kind"]
    opening, ending = {
        "TICKET": ("申请 / 售票开始", "申请 / 售票截止"),
        "RESERVATION": ("预约开始", "预约截止"),
        "GOODS": ("商品贩售开始", "商品贩售截止"),
        "PAYMENT": ("付款开始", "付款截止"),
        "PERIOD": ("活动开始", "活动结束"),
    }.get(kind, (title(node), "结束"))
    values = [(start, "start", opening)]
    if end and end != start:
        values.append((end, "end", ending))
    elif end == start and kind in {"TICKET", "RESERVATION", "GOODS", "PAYMENT"}:
        values = [(start, "point", opening + " / 截止")]
    if kind == "PAYMENT" and not end:
        values = [(start, "point", "付款截止")]
    return [
        {
            "id": node["id"] + ":" + boundary,
            "date": value.astimezone(JST).date().isoformat() if precise else value.isoformat(),
            "at": value.isoformat() if precise else None,
            "boundary": boundary,
            "label": label,
        }
        for value, boundary, label in values
        if value
    ]


def relevant(node: dict, participation: str | None) -> bool:
    # Unknown participation never means not applied or not selected.
    if node["status"] == "CANCELED":
        return True
    if participation in {"APPLIED", "WON", "PURCHASED"} and node["kind"] == "TICKET":
        return False
    if participation in {"WON", "PURCHASED"} and node["kind"] == "RESULT":
        return False
    return not (participation == "PURCHASED" and node["kind"] == "PAYMENT")


def agenda_groups(nodes: list[dict], participation: list[dict], start: date, end: date):
    states = {(p["activity_id"], p["round_key"]): p["status"] for p in participation}
    groups = {}
    ongoing = {}
    undated = 0
    for node in nodes:
        state = states.get((node["activity_id"], node["round_key"])) if node["round_key"] else None
        if not relevant(node, state):
            continue
        points = boundaries(node)
        if not points:
            undated += 1
        for point in points:
            if not start.isoformat() <= point["date"] < end.isoformat():
                continue
            key = (point["date"], node["activity_id"])
            group = groups.setdefault(
                key,
                {
                    "date": point["date"],
                    "activity_id": node["activity_id"],
                    "activity_title": node["activity_title"],
                    "activity_kind": node["activity_kind"],
                    "activity_status": node["activity_status"],
                    "actions": [],
                },
            )
            group["actions"].append({**point, "node": node, "participation": state})
        # Long-running windows are a separate state summary, not an event on day 1.
        if (
            len(points) > 1
            and points[0]["date"] < start.isoformat()
            and points[-1]["date"] >= end.isoformat()
        ):
            if node["status"] == "CONFIRMED" and node["activity_status"] not in {
                "CANCELED",
                "POSTPONED",
            }:
                ongoing[node["id"]] = {"node": node, "ends_on": points[-1]["date"]}
    for group in groups.values():
        group["actions"].sort(
            key=lambda p: (
                datetime.fromisoformat(p["at"]).astimezone(JST).isoformat()
                if p["at"]
                else p["date"] + "T99",
                p["id"],
            )
        )
    return (
        sorted(groups.values(), key=lambda g: (g["date"], g["activity_title"], g["activity_id"])),
        list(ongoing.values()),
        undated,
    )
