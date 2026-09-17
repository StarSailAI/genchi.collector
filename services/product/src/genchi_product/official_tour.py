"""Conservative reconciliation of a single, explicitly configured artist tour source."""

from __future__ import annotations

import re
from datetime import datetime

from psycopg.types.json import Jsonb

from .domain import EvidenceInput, MilestoneInput, Moment, normalize
from .naming import sync_name
from .store import Catalog
from .venues import venue_key

SOURCE_ID = "sekainoowari-tour-official"
TOUR_URL = "https://www.sekainoowari-tour.jp/"
TOUR_TITLE = "SEKAI NO OWARI ARENA TOUR 2027"
ARTIST_SLUG = "sekai-no-owari"

ROUND_NAMES = {
    "ファンクラブ長期会員(5年以上)先行": "粉丝俱乐部资深会员（5 年以上）先行抽选",
    "ファンクラブW会員先行": "粉丝俱乐部双会籍会员先行抽选",
    "ファンクラブ全会員先行": "粉丝俱乐部全体会员先行抽选",
    "オフィシャル先行": "官方先行抽选",
}


def _proof(resource: dict, excerpt: str, field: str) -> EvidenceInput:
    content = re.sub(r"\s+", "", resource.get("content") or "")
    if not excerpt or re.sub(r"\s+", "", excerpt) not in content:
        raise ValueError(f"official tour {field} has no current source quote")
    return EvidenceInput(
        source_id=resource["source_id"], external_id=resource["external_id"],
        version_hash=resource["content_hash"], url=resource["url"],
        excerpt=excerpt, field_path=field, method="parser:official-tour", verified=True,
        observed_at=resource.get("observed_at"),
    )


def _moment(value: str, end: str | None = None) -> Moment:
    return Moment(precision="TIME", starts_at=value, ends_at=end)


def _existing_pia_round(conn, activity_id: str, round_data: dict):
    if round_data["label"] != "オフィシャル先行":
        return None
    matches = conn.execute(
        """SELECT * FROM catalog_milestones WHERE activity_id=%s AND kind='TICKET'
        AND platform='pia' AND starts_at=%s AND ends_at=%s AND status='CONFIRMED'""",
        (activity_id, round_data["opensAt"], round_data["closesAt"]),
    ).fetchall()
    if len(matches) != 1 or normalize(matches[0]["title"]) != normalize("オフィシャル先行"):
        raise ValueError("official presale cannot be matched uniquely to the native Pia round")
    return matches[0]


def _stage(conn, catalog: Catalog, activity_id: str, occurrence_ids: list[str],
           resource: dict, round_data: dict, kind: str, title: str, display: str,
           moment: Moment, *, status: str = "CONFIRMED", requires: str = "NONE",
           round_key: str, notes: str | None = None, platform: str = "artist-official") -> str:
    key = f"official-tour:2027:{round_data['id']}:{kind.lower()}"
    term = {"TICKET": "reception", "RESULT": "result", "PAYMENT": "payment"}[kind]
    quote = round_data["evidence"][term]
    evidence = _proof(resource, quote, f"rounds.{round_data['id']}.{term}")
    node = MilestoneInput(
        source_key=key, kind=kind, title=title, title_zh=display,
        time=moment, status=status, requires=requires,
        round_key=round_key, scope_key=round_key, url=TOUR_URL,
        eligibility=round_data.get("eligibility") if kind == "TICKET" else None,
        notes=notes, platform=platform, evidence=evidence,
    )
    for occurrence_id in occurrence_ids:
        catalog.milestone(conn, activity_id, occurrence_id, node)
    mapped = conn.execute(
        "SELECT milestone_id FROM catalog_external_ids WHERE key=%s",
        (f"{activity_id}:{key}",),
    ).fetchone()
    if not mapped:
        raise ValueError("official tour milestone has no stable source mapping")
    sync_name(conn, "MILESTONE", mapped["milestone_id"], approved=display,
              expected_source=title)
    return mapped["milestone_id"]


def reconcile(conn, catalog: Catalog, resource: dict) -> dict:
    """Apply only after complete source/schedule/round verification in the job transaction."""
    tour = (resource.get("attributes") or {}).get("official_tour") or {}
    if (resource.get("source_id") != SOURCE_ID or resource.get("url") != TOUR_URL or
            tour.get("title") != TOUR_TITLE):
        raise ValueError("unrecognized official tour source")
    events, rounds = tour.get("events") or [], tour.get("rounds") or []
    if len(events) != 23 or set(row.get("label") for row in rounds) != set(ROUND_NAMES):
        raise ValueError("official tour schedule changed; review before publishing")
    activity_rows = conn.execute(
        "SELECT * FROM catalog_activities WHERE official_url=%s AND title=%s AND publication='PUBLISHED' FOR UPDATE",
        (TOUR_URL, TOUR_TITLE),
    ).fetchall()
    if len(activity_rows) != 1:
        raise ValueError("official tour has no unique published activity")
    activity_id = activity_rows[0]["id"]
    existing = conn.execute(
        "SELECT * FROM catalog_occurrences WHERE activity_id=%s AND status<>'SUPERSEDED'",
        (activity_id,),
    ).fetchall()
    if len(existing) != len(events):
        raise ValueError("official and catalog performance counts differ")
    index = {}
    for occurrence in existing:
        key = (occurrence["starts_at"], venue_key(occurrence["venue"], TOUR_TITLE, "2027"))
        if key in index:
            raise ValueError("catalog has duplicate tour performance identity")
        index[key] = occurrence["id"]
    occurrence_ids = []
    for event in events:
        _proof(resource, event["evidence"], f"performances.{event['id']}")
        when = datetime.fromisoformat(event["startsAt"])
        key = (when, venue_key(event["venue"], TOUR_TITLE, "2027"))
        match = index.get(key)
        if not match or event["city"] != next(o["city"] for o in existing if o["id"] == match):
            raise ValueError("official tour performance does not match its catalog occurrence")
        occurrence_ids.append(match)
    if len(set(occurrence_ids)) != len(existing):
        raise ValueError("official tour performance mapping is not one-to-one")
    title_evidence = catalog.evidence(conn, activity_id,
                                      _proof(resource, TOUR_TITLE, "activity.title"))
    conn.execute(
        """INSERT INTO catalog_subjects(slug,name,name_zh,aliases,description,color,subject_type)
        VALUES(%s,%s,%s,%s,%s,%s,'ARTIST') ON CONFLICT(slug) DO NOTHING""",
        (ARTIST_SLUG, "SEKAI NO OWARI", "SEKAI NO OWARI", Jsonb(["世界の終わり", "世终"]),
         "日本音乐组合；关注其巡演、票务与线下活动。", "#334caa"),
    )
    conn.execute(
        """INSERT INTO catalog_activity_subjects(activity_id,subject_slug,relation_kind,
        participant_name,evidence_id,verified) VALUES(%s,%s,'PERFORMER',%s,%s,TRUE)
        ON CONFLICT(activity_id,subject_slug) DO UPDATE SET relation_kind='PERFORMER',
        participant_name=EXCLUDED.participant_name,evidence_id=EXCLUDED.evidence_id,verified=TRUE""",
        (activity_id, ARTIST_SLUG, "SEKAI NO OWARI", title_evidence),
    )
    created = []
    for round_data in rounds:
        label = round_data["label"]
        _proof(resource, label, f"rounds.{round_data['id']}.label")
        display = ROUND_NAMES[label]
        existing_pia = _existing_pia_round(conn, activity_id, round_data)
        round_key = (existing_pia["round_key"] if existing_pia else
                     f"official-tour:{round_data['id']}")
        if existing_pia:
            catalog.evidence(conn, activity_id,
                             _proof(resource, round_data["evidence"]["reception"],
                                    f"rounds.{round_data['id']}.reception"), existing_pia["id"])
            results = conn.execute(
                """SELECT id FROM catalog_milestones WHERE activity_id=%s AND kind='RESULT'
                AND round_key=%s AND starts_at=%s""",
                (activity_id, round_key, round_data["resultAt"]),
            ).fetchall()
            if len(results) != 1:
                raise ValueError("official presale result does not match native Pia")
            catalog.evidence(conn, activity_id,
                             _proof(resource, round_data["evidence"]["result"],
                                    f"rounds.{round_data['id']}.result"), results[0]["id"])
        else:
            created.append(_stage(
                conn, catalog, activity_id, occurrence_ids, resource, round_data,
                "TICKET", label, display,
                _moment(round_data["opensAt"], round_data["closesAt"]),
                round_key=round_key,
            ))
            if round_data.get("resultAt"):
                created.append(_stage(
                    conn, catalog, activity_id, occurrence_ids, resource, round_data,
                    "RESULT", f"{label} · 当落発表（予定）",
                    f"{display} · 抽选结果公布（预计）",
                    _moment(round_data["resultAt"]), status="REVIEW", requires="APPLIED",
                    round_key=round_key, notes="官方标注为预计时间；请以最新公告为准",
                ))
        if round_data.get("paymentClosesAt"):
            created.append(_stage(
                conn, catalog, activity_id, occurrence_ids, resource, round_data,
                "PAYMENT", f"{label} · 入金期限", f"{display} · 付款截止",
                _moment(round_data["paymentClosesAt"]), requires="WON",
                round_key=round_key,
                notes="入金开始时间为预计；截止时间按官方公告记录"
                if round_data.get("paymentStartPlanned") else None,
                platform="pia" if existing_pia else "artist-official",
            ))
    return {"activity_id": activity_id, "occurrences": len(occurrence_ids),
            "rounds": len(rounds), "new_or_updated_nodes": len(created)}
