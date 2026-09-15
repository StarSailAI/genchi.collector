"""Deterministic, read-only evidence retrieval, independent of an LLM provider.

Rankings are discovery aids, never event facts. Only stored reviewed nodes and
verbatim source passages leave this layer; it does not extract or publish dates.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from genchi_normalizer.glossary import load_glossary
from pydantic import BaseModel, ConfigDict, Field


class EvidenceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    terms: list[str] = Field(min_length=1, max_length=1,
        description="Exactly ONE entity name, preferably original Japanese. No live/ticket/受付 keywords, parent franchise, or speculative aliases; the server resolves known aliases.")
    focus: list[str] = Field(default_factory=list, max_length=3)
    intent: Literal["performance", "festival", "application_deadline", "payment_deadline",
                    "reservation_deadline", "period_end", "general"] = "general"
    time_scope: Literal["upcoming", "past", "any"] = "upcoming"


# Intent vocabulary, not project/question-specific answers or entity aliases.
VOCABULARY = {
    "performance": ("公演", "ライブ", "LIVE", "開演", "concert"),
    "festival": ("フェス", "FES", "FESTIVAL", "出演", "音楽祭"),
    "application_deadline": ("抽選", "受付", "先行", "申込", "締切"),
    "payment_deadline": ("入金", "支払", "payment", "期限"),
    "reservation_deadline": ("予約", "予約受付", "締切", "reservation"),
    "period_end": ("開催期間", "終了", "最終日", "会期"),
    "general": (),
}
NODE_KINDS = {
    "performance": ["START"], "festival": ["START", "PERIOD"],
    "application_deadline": ["TICKET"], "payment_deadline": ["PAYMENT"],
    "reservation_deadline": ["RESERVATION"], "period_end": ["PERIOD"], "general": [],
}
WARNINGS = ("中止", "延期", "変更", "訂正", "canceled", "cancelled", "postponed")
PRIMARY_TYPES = {"official_site", "asobi_ticket", "eplus_ticket", "pia_ticket", "lawson_ticket"}


def normalized(value):
    return unicodedata.normalize("NFKC", value).casefold().strip()


def unique(values, limit=16, minimum=2):
    found = {}
    for value in values:
        if isinstance(value, str) and minimum <= len(value.strip()) <= 80:
            found.setdefault(value.casefold().strip(), value.strip())
    return list(found.values())[:limit]


def expand_aliases(terms, subjects):
    """Exact alias equivalence only. Never expand a band into its parent IP."""
    result = unique(terms)
    wanted = {normalized(term) for term in result}
    groups = {}
    for alias, preferred in load_glossary()["terms"].items():
        groups.setdefault(preferred, [preferred]).append(alias)
    for subject in subjects:
        names = unique([subject["name"], subject.get("name_zh"), *(subject.get("aliases") or [])])
        if wanted.intersection(map(normalized, names)):
            result.extend(names)
    for names in groups.values():
        if wanted.intersection(map(normalized, names)):
            result.extend(names)
    return unique([*result, *(normalized(value) for value in result)], limit=8)


def passage_windows(text, terms, focus, *, limit=3, width=1200):
    """Bounded overlapping verbatim windows; keep context and change notices.

    Offsets refer to the original snapshot, not normalized text. No synthetic
    ellipses, reconstructed sentences or model-created quote strings.
    """
    if not text:
        return []
    text = text[:60000]
    windows = []
    for start in range(0, len(text), width - 200):
        chunk = text[start:start + width]
        body = normalized(chunk)
        score = sum(normalized(t) in body for t in terms) * 3
        score += sum(normalized(t) in body for t in focus) * 2
        score += bool(re.search(r"20\d{2}[年/.-]\s*\d{1,2}", body))
        warning = any(word in body for word in WARNINGS)
        windows.append(dict(offset=start, text=chunk, score=score, warning=warning))
        if start + width >= len(text):
            break
    # Intro identifies scope; the most relevant body section usually has dates.
    selected = [windows[0]]
    warnings = [w for w in windows if w["warning"] and w["offset"]]
    if warnings:
        selected.append(max(warnings, key=lambda w: w["score"]))
    for window in sorted(windows, key=lambda w: (-w["score"], w["offset"])):
        if len(selected) >= limit:
            break
        if window not in selected:
            selected.append(window)
    return [{"offset": w["offset"], "text": w["text"]} for w in sorted(selected, key=lambda w: w["offset"])]


def document_score(row, terms, focus):
    title, body = normalized(row["title"] or ""), normalized(row.get("content") or "")
    # Aliases are alternatives, not independent votes. Repetition cannot inflate
    # ranking. Co-located entity and intent outrank a shared footer/nav mention.
    title_hit = any(normalized(t) in title for t in terms)
    title_focus = sum(normalized(t) in title for t in focus)
    proximity = 0
    for start in range(0, len(body), 600):
        window = body[start:start + 1000]
        if any(normalized(t) in window for t in terms):
            proximity = max(proximity, sum(normalized(t) in window for t in focus))
    official = row.get("source_type") in PRIMARY_TYPES
    return 12 * title_hit + 4 * min(title_focus, 3) + 3 * min(proximity, 4) + 4 * official


def native_time_rank(row, query, now):
    # Native adapter timestamps are retrieval hints, not newly reviewed facts.
    if query is None or query.time_scope == "any":
        return 0
    key = {"application_deadline": "受付終了", "payment_deadline": "支払終了"}.get(query.intent)
    if row.get("source_type") != "asobi_ticket" or not key:
        return 0
    match = re.search(r"(?m)^" + key + r": ([^\n]+)", (row.get("content") or "")[:1200])
    if not match:
        return 0
    try:
        bound = datetime.fromisoformat(match[1])
        if bound.tzinfo is None:
            return 0
        matches = bound >= now if query.time_scope == "upcoming" else bound < now
        return 1 if matches else -1
    except ValueError:
        return 0


def select_documents(rows, terms, focus, limit=6, query=None, now=None):
    # Prefer the latest observed snapshot of an identical URL before ranking;
    # keep different URLs/rounds separate, and diversify hosts when available.
    latest = {}
    for row in sorted(rows, key=lambda r: (str(r.get("observed_at") or ""), r["id"]), reverse=True):
        latest.setdefault(row["url"], row)
    ranked = sorted(latest.values(), key=lambda r: (r.get("source_type") in PRIMARY_TYPES,
                    native_time_rank(r, query, now),
                    document_score(r, terms, focus)), reverse=True)
    selected, deferred, hosts = [], [], {}
    for row in ranked:
        host = urlsplit(row["url"]).hostname
        if hosts.get(host, 0) >= 3:
            deferred.append(row)
        else:
            selected.append(row)
            hosts[host] = hosts.get(host, 0) + 1
    return (selected + deferred)[:limit]


def search_evidence(store, query, now):
    # Local import avoids making the provider harness a dependency of query types.
    from .ask_agent import patterns, public_url

    patterns(query.terms)
    patterns(query.focus, minimum=1)
    if store.subjects is None:
        with store.connection() as conn:
            store.subjects = conn.execute("SELECT name,name_zh,aliases FROM catalog_subjects ORDER BY slug LIMIT 1000").fetchall()
    terms = expand_aliases(query.terms, store.subjects)
    focus = unique([*query.focus, *VOCABULARY[query.intent]], minimum=1)
    signature = (tuple(sorted(map(normalized, terms))), tuple(sorted(map(normalized, focus))), query.intent, query.time_scope, str(now))
    if signature in store.search_cache:
        return dict(store.search_cache[signature], cached=True)
    words, boosts = patterns(terms), patterns(focus, minimum=1)
    with store.connection() as conn:
        rows = conn.execute("""WITH documents AS (
          SELECT r.*,concat_ws(' · ',attributes#>>'{asobi_ticket,booth,attributes,name}',
            attributes#>>'{ticket_page,events,0,name}',title) AS search_title
          FROM allfeeds.resources r)
          SELECT id,search_title AS title,url,source_id,content_hash,observed_at,published_at,
            attributes->>'source_type' AS source_type,attributes->>'source_role' AS source_role,
            left(content,60000) AS content,length(content) AS content_length,
            (CASE WHEN search_title ILIKE ANY(%s) THEN 12 ELSE 0 END
             + (SELECT count(*) FROM unnest(%s::text[]) p WHERE search_title ILIKE p)*4
             + (SELECT count(*) FROM unnest(%s::text[]) p WHERE left(content,12000) ILIKE p)*2) AS rank
          FROM documents WHERE search_title ILIKE ANY(%s) OR content ILIKE ANY(%s)
          ORDER BY rank DESC,observed_at DESC,id DESC LIMIT 49""", (words, boosts, boosts, words, words)).fetchall()
    truncated = len(rows) > 48
    candidates = [r for r in rows[:48] if public_url(r["url"]) and r.get("content")]
    items = []
    for row in select_documents(candidates, terms, focus, query=query, now=now):
        handle = store.add(f"raw:{row['id']}", dict(row, kind="document", activity_id=None))
        passages = store.expose_passages(handle, passage_windows(row["content"], terms, focus))
        items.append(dict(source_id=handle, title=row["title"], url=row["url"],
            source_type=row["source_type"], source_role=row["source_role"],
            observed_at=row["observed_at"], version=row["content_hash"], passages=passages,
            partial=sum(len(p["text"]) for p in passages) < row["content_length"]))
    nodes = verified_nodes(store, words, query, now)
    # Reviewed evidence is a separate, explicitly labelled provenance channel.
    for node in nodes:
        proofs = node.pop("proofs")
        node["evidence"] = []
        for proof in proofs:
            if not public_url(proof["url"]) or not proof["excerpt"]:
                continue
            handle = store.add(f"reviewed:{proof['id']}", dict(id=proof["id"], title=node["activity_title"],
                content=proof["excerpt"], url=proof["url"], observed_at=proof["observed_at"],
                content_hash=proof["version_hash"], kind="activity", activity_id=node["activity_id"],
                source_type="reviewed_evidence", source_role=None))
            node["evidence"].append(dict(source_id=handle, source_type="reviewed_evidence", url=proof["url"],
                passages=store.expose_passages(handle, [{"offset": 0, "text": proof["excerpt"]}])) )
    result = dict(items=items, verified_nodes=nodes, aliases=terms, intent=query.intent,
        truncated=truncated or len(candidates) > len(items),
        coverage="Collected snapshots, not live web or exhaustive event coverage. Rankings do not establish dates or appearances.",
        guidance="Passages below are already read and citable by source_id + passage_id. Answer now if sufficient; use read_source only for missing context. Verified nodes retain round/scope/precision; a TICKET kind alone does not prove lottery rather than first-come sale.")
    store.search_cache[signature] = result
    return dict(result)


def verified_nodes(store, words, query, now):
    kinds = NODE_KINDS[query.intent]
    end_intent = query.intent.endswith("deadline") or query.intent == "period_end"
    # Use DATE only for comparison, never expose a fabricated midnight timestamp.
    boundary_at = "m.ends_at" if end_intent else "m.starts_at"
    boundary_on = "m.ends_on" if end_intent else "m.starts_on"
    future = f"(({boundary_at} >= %s AND m.precision='TIME') OR ({boundary_on} >= (%s AT TIME ZONE 'Asia/Tokyo')::date AND m.precision='DATE'))"
    past = f"(({boundary_at} < %s AND m.precision='TIME') OR ({boundary_on} < (%s AT TIME ZONE 'Asia/Tokyo')::date AND m.precision='DATE'))"
    scope = future if query.time_scope == "upcoming" else past
    with store.connection() as conn:
        return conn.execute(f"""SELECT a.id AS activity_id,a.title AS activity_title,a.kind AS activity_kind,
          m.id,m.title,m.kind,m.precision,m.starts_at,m.ends_at,m.starts_on,m.ends_on,m.round_key,m.eligibility,
          (SELECT jsonb_agg(x) FROM (SELECT o.label,o.venue,o.city,o.status
            FROM catalog_milestone_scopes s JOIN catalog_occurrences o ON o.id=s.occurrence_id
            WHERE s.milestone_id=m.id AND o.status<>'SUPERSEDED' ORDER BY o.id LIMIT 12) x) AS occurrence_scope,
          (SELECT jsonb_agg(x) FROM (SELECT subject_slug,relation_kind,participant_name,scope_note,verified
            FROM catalog_activity_subjects WHERE activity_id=a.id ORDER BY subject_slug LIMIT 12) x) AS relationships,
          (SELECT jsonb_agg(x) FROM (SELECT id,url,left(excerpt,2000) AS excerpt,observed_at,version_hash
            FROM catalog_evidence WHERE milestone_id=m.id AND verified ORDER BY observed_at DESC,id LIMIT 2) x) AS proofs
          FROM catalog_milestones m JOIN catalog_activities a ON a.id=m.activity_id
          WHERE a.publication='PUBLISHED' AND a.status IN ('ANNOUNCED','SCHEDULED','ENDED') AND a.attendance IN ('OFFLINE','HYBRID')
            AND m.status='CONFIRMED' AND (cardinality(%s::text[])=0 OR m.kind=ANY(%s))
            AND NOT EXISTS(SELECT 1 FROM catalog_milestone_scopes ms JOIN catalog_occurrences oc ON oc.id=ms.occurrence_id
              WHERE ms.milestone_id=m.id AND oc.status IN ('CANCELED','SUPERSEDED'))
            AND EXISTS(SELECT 1 FROM catalog_evidence e WHERE e.milestone_id=m.id AND e.verified)
            AND (a.title ILIKE ANY(%s) OR a.title_zh ILIKE ANY(%s) OR EXISTS(
              SELECT 1 FROM catalog_activity_subjects l JOIN catalog_subjects s ON s.slug=l.subject_slug
              WHERE l.activity_id=a.id AND (l.participant_name ILIKE ANY(%s) OR s.name ILIKE ANY(%s)
                OR s.name_zh ILIKE ANY(%s) OR s.aliases::text ILIKE ANY(%s))))
            AND (%s='any' OR {scope})
            AND (%s<>'upcoming' OR a.status<>'ENDED')
          ORDER BY COALESCE({boundary_at},{boundary_on}::timestamp AT TIME ZONE 'Asia/Tokyo')
            {"DESC" if query.time_scope == "past" else "ASC"} NULLS LAST,m.id LIMIT 8""",
          (kinds, kinds, words, words, words, words, words, words, query.time_scope, now, now, query.time_scope)).fetchall()
