"""Daily, evidence-backed homepage countdowns; no model or collection calls."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .localization import display_name, request_locale

JST = ZoneInfo("Asia/Tokyo")


def countdown(row, now):
    """DATE retains day precision. Never infer a ticket deadline from its opening."""
    deadline = row["kind"] in {"TICKET", "RESERVATION", "PERIOD"}
    if row["precision"] == "TIME":
        target = row["ends_at"] if deadline else row["starts_at"]
        if not target or target <= now:
            return None
        target_day = target.astimezone(JST).date()
    elif row["precision"] == "DATE":
        target = row["ends_on"] if deadline else row["starts_on"]
        if not target:
            return None
        target_day = target if isinstance(target, date) else date.fromisoformat(target)
        if target_day < now.astimezone(JST).date():
            return None
    else:
        return None
    remaining = (target_day - now.astimezone(JST).date()).days
    if remaining > 90:
        return None
    return {
        **row,
        "target_date": target_day.isoformat(),
        "target_at": target.isoformat() if row["precision"] == "TIME" else None,
        "boundary": "deadline" if row["kind"] in {"TICKET", "RESERVATION"}
        else "end" if deadline else "start",
        "days_remaining": remaining,
    }


def candidates(conn, now):
    rows = conn.execute("""
        SELECT m.id,m.activity_id,m.title AS milestone_title,m.title_zh AS milestone_title_zh,
          m.kind,m.precision,m.starts_at,m.ends_at,m.starts_on,m.ends_on,m.revision,
          a.title,a.title_zh,a.kind AS activity_kind,a.updated_at,
          (SELECT s.subject_slug FROM catalog_activity_subjects s
           WHERE s.activity_id=a.id ORDER BY s.verified DESC,s.subject_slug LIMIT 1) AS subject_slug,
          (SELECT s.subject_type FROM catalog_activity_subjects l
           JOIN catalog_subjects s ON s.slug=l.subject_slug
           WHERE l.activity_id=a.id ORDER BY l.verified DESC,s.slug LIMIT 1) AS subject_type,
          (SELECT count(*) FROM genchi_private.follows f
           WHERE f.target_type='ACTIVITY' AND f.target_id=a.id) AS follow_count,
          (SELECT count(DISTINCT e.url) FROM catalog_evidence e
           WHERE e.activity_id=a.id AND e.verified) AS source_count
        FROM catalog_milestones m JOIN catalog_activities a ON a.id=m.activity_id
        WHERE a.publication='PUBLISHED' AND a.attendance IN ('OFFLINE','HYBRID')
          AND a.status IN ('ANNOUNCED','SCHEDULED') AND m.status='CONFIRMED'
          AND (m.kind IN ('TICKET','RESERVATION','START')
            OR (m.kind='PERIOD' AND a.kind IN ('CAFE','POPUP','EXHIBITION')))
          AND EXISTS(SELECT 1 FROM catalog_evidence e WHERE e.milestone_id=m.id AND e.verified)
          AND COALESCE(m.ends_at,(m.ends_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo',
              m.starts_at,(m.starts_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo')>%s
        ORDER BY COALESCE(m.ends_at,m.ends_on::timestamp AT TIME ZONE 'Asia/Tokyo',
              m.starts_at,m.starts_on::timestamp AT TIME ZONE 'Asia/Tokyo'),m.id LIMIT 600
    """, (now,)).fetchall()
    return [item for row in rows if (item := countdown(row, now))]


def select_music_cards(items):
    """Choose timely, well-supported Japanese music activities for the homepage."""
    # A 30-day window keeps the right rail actionable; popularity breaks ties within it.
    candidates = [row for row in items if row["days_remaining"] <= 30]
    ordered = sorted(
        candidates,
        key=lambda row: (
            -(
                min(row["follow_count"], 20) * 4
                + min(row["source_count"], 5) * 3
                + max(0, 30 - row["days_remaining"])
            ),
            row["days_remaining"],
            row["id"],
        ),
    )
    selected, activities = [], set()
    for row in ordered:
        if row["activity_id"] in activities:
            continue
        selected.append(row)
        activities.add(row["activity_id"])
        if len(selected) == 3:
            break
    return selected


def select_cards(items):
    # Source corroboration and actual follows are signals, not invented popularity.
    ordered = sorted(items, key=lambda r: (
        -(min(r["follow_count"], 20) * 3 + min(r["source_count"], 5) * 2
          + (8 if r["days_remaining"] <= 14 else 0)),
        r["days_remaining"], r["id"],
    ))
    selected, activities, subjects, boundaries = [], set(), {}, {}
    for diverse in (True, False):
        for row in ordered:
            if row["activity_id"] in activities:
                continue
            subject = row["subject_slug"] or row["activity_id"]
            if diverse and (subjects.get(subject, 0) >= 2 or boundaries.get(row["boundary"], 0) >= 3):
                continue
            selected.append(row)
            activities.add(row["activity_id"])
            subjects[subject] = subjects.get(subject, 0) + 1
            boundaries[row["boundary"]] = boundaries.get(row["boundary"], 0) + 1
            if len(selected) == 6:
                return selected
    return selected


def featured(catalog):
    now = datetime.now(UTC)
    today = now.astimezone(JST).date()
    with catalog.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || ':home-features',0))")
        saved = conn.execute("SELECT * FROM genchi_private.home_features WHERE id=TRUE").fetchone()
        current = candidates(conn, now)
        by_id = {row["id"]: row for row in current}
        selected = [by_id[key] for key in (saved["milestone_ids"] if saved else []) if key in by_id]
        available_activities = len({row["activity_id"] for row in current})
        if not saved or saved["selection_date"] != today or len(selected) < min(6, available_activities):
            selected = select_cards(current)
            saved = conn.execute("""
                INSERT INTO genchi_private.home_features(id,selection_date,milestone_ids)
                VALUES(TRUE,%s,%s) ON CONFLICT(id) DO UPDATE SET
                selection_date=EXCLUDED.selection_date,milestone_ids=EXCLUDED.milestone_ids,updated_at=NOW()
                RETURNING *
            """, (today, Jsonb([row["id"] for row in selected]))).fetchone()
        # Read live facts each time so cancellations, corrections and expiry are never cached for a day.
        items = []
        for row in selected:
            item = {k: v for k, v in row.items() if k not in {"follow_count", "source_count"}}
            item["milestone_title_localized"] = display_name(
                row["milestone_title"], row["milestone_title_zh"], request_locale.get())
            items.append(item)
        music_items = []
        for row in select_music_cards(
            item
            for item in current
            if item.get("activity_kind") in {"LIVE", "FESTIVAL"}
            and item.get("subject_type") != "FRANCHISE"
        ):
            item = {
                k: v
                for k, v in row.items()
                if k not in {"follow_count", "source_count", "subject_type"}
            }
            item["milestone_title_localized"] = display_name(
                row["milestone_title"], row["milestone_title_zh"], request_locale.get()
            )
            music_items.append(item)
        return {
            "items": items,
            "music_items": music_items,
            "selected_at": saved["updated_at"],
            "as_of": now,
        }
