"""Conservative cross-source identity rules, independent of translated display names."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .domain import JST, ActivityInput, MilestoneInput, canonical_url, normalize

SECONDARY_HOSTS = {
    "collabo-cafe.com", "anime.eiga.com", "eventernote.com", "www.eventernote.com",
    "nijimen.kusuguru.co.jp", "natalie.mu", "spice.eplus.jp", "www.livefans.jp",
    "x.com", "twitter.com", "www.youtube.com", "youtu.be",
}


def event_reference(url: str | None) -> str | None:
    """An exact event page is a clue; a homepage, account or search is not an identity."""
    clean = canonical_url(url)
    if not clean:
        return None
    p = urlsplit(clean)
    if p.hostname in SECONDARY_HOSTS or p.path.rstrip("/") in {"", "/news", "/event", "/events", "/live", "/ticket", "/tickets"}:
        return None
    if any(part in p.path.lower().split("/") for part in {"search", "search_all.do", "word", "category", "tag", "tag.do"}):
        return None
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), urlencode(sorted(query)), ""))


def assess_activity(item: ActivityInput, existing: dict) -> dict | None:
    reference = event_reference(item.url)
    if not reference or reference != event_reference(existing.get("official_url")):
        return None
    known_subjects = set(existing.get("subjects") or [])
    if known_subjects and item.subject_slugs and not known_subjects.intersection(item.subject_slugs):
        return None
    kind_ok = item.kind == existing.get("kind") or "OTHER" in {item.kind, existing.get("kind")}
    day = item.time.anchor()
    end = str(item.time.ends_on or (item.time.ends_at.astimezone(JST).date() if item.time.ends_at else day))
    intervals = existing.get("dates") or []
    dated = [d for d in intervals if d.get("start")]
    overlap = day != "TBD" and any(str(d["start"]) <= end and str(d.get("end") or d["start"]) >= day for d in dated)
    same_title = normalize(item.title) == normalize(existing["title"])
    # Reused cafe/tour URLs must not combine different editions. Unknown dates or
    # incompatible scopes remain suggestions for an editor, not automatic merges.
    return {"activity_id": existing["id"], "title": existing["title"],
            "strength": "exact_event_and_dates" if overlap and kind_ok else "review",
            "same_original_title": same_title, "reference": reference}


def find_activity_matches(conn, item: ActivityInput) -> list[dict]:
    reference = event_reference(item.url)
    if not reference:
        return []
    # A narrow host/path lookup also finds links whose only difference is tracking
    # or query ordering. Python compares full canonical references afterward.
    prefix = reference.split("?", 1)[0]
    rows = conn.execute(
        """SELECT a.id,a.title,a.kind,a.official_url,
        ARRAY(SELECT subject_slug FROM catalog_activity_subjects s WHERE s.activity_id=a.id) subjects,
        (SELECT jsonb_agg(jsonb_build_object('start',COALESCE(o.starts_on,(o.starts_at AT TIME ZONE 'Asia/Tokyo')::date),
          'end',COALESCE(o.ends_on,(o.ends_at AT TIME ZONE 'Asia/Tokyo')::date,o.starts_on,(o.starts_at AT TIME ZONE 'Asia/Tokyo')::date)))
          FROM catalog_occurrences o WHERE o.activity_id=a.id) dates
        FROM catalog_activities a WHERE rtrim(split_part(a.official_url,'?',1),'/')=rtrim(%s,'/')
        AND a.publication <> 'REJECTED' LIMIT 100""", (prefix,),
    ).fetchall()
    return [match for row in rows if (match := assess_activity(item, row))]


def same_milestone_fact(item: MilestoneInput, row: dict, *, same_occurrence: bool) -> bool:
    if item.kind != row["kind"] or any(row.get(k) != v for k, v in item.time.model_dump().items()):
        return False
    if item.status != row["status"] or item.requires != row["requires"]:
        return False
    if item.kind in {"START", "PERIOD", "DOORS"}:
        return same_occurrence
    if item.kind not in {"TICKET", "RESERVATION", "RESULT", "PAYMENT", "GOODS"}:
        return False
    if not item.url or canonical_url(item.url) != canonical_url(row.get("url")):
        return False
    if normalize(item.title) != normalize(row["title"]) or item.eligibility != row.get("eligibility"):
        return False
    if item.platform and row.get("platform") and item.platform != row["platform"]:
        return False
    # A second ticket product can share name and sales times. Price/conditions
    # are identity-bearing evidence too; missing fields are not equality.
    left, right = item.details, row.get("details") or {}
    if left != right or item.notes != row.get("notes"):
        return False
    return True
