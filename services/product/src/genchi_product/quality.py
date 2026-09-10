"""Read-only catalog quality audit and narrowly scoped, idempotent editorial repairs."""

from __future__ import annotations

import argparse
import json

from psycopg.types.json import Jsonb

from .domain import fingerprint
from .store import Catalog

ANIERA_ID = "f65af75c-d995-4827-9ec8-dd4a23e4fe31"
ANIERA_OFFICIAL = "https://bang-dream.com/events/anierafesta2026"
ANIERA_ORGANIZER = "https://aniera-festa.com"
ANIERA_PROOF = (
    "「ナガノアニエラフェスタ2026」にBanG Dream!プロジェクトより、RAISE A SUILENの出演が決定！ "
    "9月20日(日) TEMPEST STAGEにRAISE A SUILENが出演"
)


def audit(catalog: Catalog, limit: int = 50) -> dict:
    queries = {
        "published_without_verified_evidence": """
            SELECT a.id,a.title,a.official_url FROM catalog_activities a
            WHERE a.publication='PUBLISHED' AND NOT EXISTS (
              SELECT 1 FROM catalog_evidence e WHERE e.activity_id=a.id AND e.verified)
            ORDER BY a.updated_at DESC LIMIT %s""",
        "community_url_used_as_official": """
            SELECT id,title,official_url FROM catalog_activities
            WHERE publication='PUBLISHED' AND official_url ~* '^https?://([^/]+\\.)?bandori\\.fans/'
            ORDER BY updated_at DESC LIMIT %s""",
        "unverified_subject_relation": """
            SELECT a.id,a.title,l.subject_slug,l.relation_kind,l.participant_name,l.scope_note
            FROM catalog_activity_subjects l JOIN catalog_activities a ON a.id=l.activity_id
            WHERE a.publication='PUBLISHED' AND NOT l.verified
            ORDER BY a.updated_at DESC LIMIT %s""",
        "transport_as_occurrence": """
            SELECT a.id,a.title,o.id AS occurrence_id,o.label,o.venue
            FROM catalog_occurrences o JOIN catalog_activities a ON a.id=o.activity_id
            WHERE o.status<>'SUPERSEDED' AND concat_ws(' ',o.label,o.venue) ~*
              '(シャトル|送迎|往復バス|⇔|駅.+会場|会場.+駅)'
            ORDER BY a.updated_at DESC LIMIT %s""",
        "evidence_source_url_mismatch": """
            SELECT e.activity_id,a.title,e.source_id,e.url FROM catalog_evidence e
            JOIN catalog_activities a ON a.id=e.activity_id
            WHERE (e.source_id ILIKE '%%eplus%%' AND e.url IS NOT NULL AND e.url !~* 'eplus\\.jp')
               OR (e.source_id ILIKE '%%pia%%' AND e.url IS NOT NULL AND e.url !~* 'pia\\.jp')
               OR (e.source_id ILIKE '%%lawson%%' AND e.url IS NOT NULL AND e.url !~* '(l-tike\\.com|lawson)')
            ORDER BY e.observed_at DESC LIMIT %s""",
    }
    result = {}
    with catalog.connect() as conn:
        for name, query in queries.items():
            rows = conn.execute(query, (limit,)).fetchall()
            count_query = f"SELECT count(*) AS count FROM ({query.rsplit('ORDER BY', 1)[0]}) q"
            result[name] = {
                "count": conn.execute(count_query).fetchone()["count"],
                "examples": rows,
            }
    return result


def repair_aniera(catalog: Catalog, *, apply: bool) -> dict:
    """Repair the reviewed golden case without broad or title-only deletion."""
    with catalog.connect() as conn, conn.transaction():
        activity = conn.execute(
            "SELECT id,title,official_url FROM catalog_activities WHERE id=%s FOR UPDATE", (ANIERA_ID,)
        ).fetchone()
        if not activity:
            raise RuntimeError("Aniera Festa activity was not found")
        transport = conn.execute("""
            SELECT id,label,venue,status FROM catalog_occurrences WHERE activity_id=%s
            AND status<>'SUPERSEDED' AND concat_ws(' ',label,venue) ~*
            '(シャトル|送迎|往復バス|⇔|駅.+会場|会場.+駅)' ORDER BY id
        """, (ANIERA_ID,)).fetchall()
        result = {
            "activity": activity,
            "official_url_after": ANIERA_ORGANIZER + "/",
            "affiliation": {"subject_slug": "bang-dream", "relation_kind": "PERFORMER",
                            "participant_name": "RAISE A SUILEN", "scope_note": "2026-09-20"},
            "transport_occurrences_to_supersede": transport,
            "applied": apply,
        }
        if not apply:
            return result
        evidence_id = fingerprint(f"{ANIERA_ID}|bang-dream|{ANIERA_OFFICIAL}|{ANIERA_PROOF}")
        conn.execute("""
            INSERT INTO catalog_evidence(id,activity_id,url,excerpt,field_path,method,verified)
            VALUES(%s,%s,%s,%s,'subjects.bang-dream','editorial:official-crosscheck',TRUE)
            ON CONFLICT(id) DO UPDATE SET verified=TRUE,observed_at=NOW()
        """, (evidence_id, ANIERA_ID, ANIERA_OFFICIAL, ANIERA_PROOF))
        conn.execute("UPDATE catalog_activities SET official_url=%s,updated_at=NOW() WHERE id=%s",
                     (ANIERA_ORGANIZER + "/", ANIERA_ID))
        conn.execute("""
            UPDATE catalog_activity_subjects SET relation_kind='PERFORMER',
              participant_name='RAISE A SUILEN',scope_note='2026-09-20',
              evidence_id=%s,verified=TRUE
            WHERE activity_id=%s AND subject_slug='bang-dream'
        """, (evidence_id, ANIERA_ID))
        occurrence_ids = [row["id"] for row in transport]
        if occurrence_ids:
            conn.execute("UPDATE catalog_occurrences SET status='SUPERSEDED' WHERE id=ANY(%s)",
                         (occurrence_ids,))
            milestones = conn.execute("""
                SELECT DISTINCT s.milestone_id FROM catalog_milestone_scopes s
                WHERE s.occurrence_id=ANY(%s) AND NOT EXISTS (
                  SELECT 1 FROM catalog_milestone_scopes active
                  JOIN catalog_occurrences o ON o.id=active.occurrence_id
                  WHERE active.milestone_id=s.milestone_id AND o.status<>'SUPERSEDED')
            """, (occurrence_ids,)).fetchall()
            milestone_ids = [row["milestone_id"] for row in milestones]
            if milestone_ids:
                conn.execute("UPDATE catalog_milestones SET status='SUPERSEDED',updated_at=NOW() WHERE id=ANY(%s)",
                             (milestone_ids,))
                conn.execute("""UPDATE genchi_private.mail_queue SET status='CANCELED'
                    WHERE milestone_id=ANY(%s) AND status IN ('PENDING','SENDING')""", (milestone_ids,))
        conn.execute("""INSERT INTO catalog_changes(activity_id,kind,summary,before_value,after_value,notify)
            VALUES(%s,'DATA_REPAIRED','以官方公告核验出演关系，并移除误作场次的接驳交通',%s,%s,FALSE)""",
            (ANIERA_ID, Jsonb({"official_url": activity["official_url"]}), Jsonb(result)))
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    audit_parser = sub.add_parser("audit")
    audit_parser.add_argument("--limit", type=int, default=50)
    repair_parser = sub.add_parser("repair-aniera")
    repair_parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    catalog = Catalog()
    result = audit(catalog, args.limit) if args.command == "audit" else repair_aniera(catalog, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
