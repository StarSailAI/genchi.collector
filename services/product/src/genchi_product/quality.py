"""Read-only catalog quality audit and narrowly scoped, idempotent editorial repairs."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .domain import ActivityInput, EvidenceInput, MilestoneInput, Moment, fingerprint
from .store import Catalog

ANIERA_ID = "f65af75c-d995-4827-9ec8-dd4a23e4fe31"
ANIERA_OFFICIAL = "https://bang-dream.com/events/anierafesta2026"
ANIERA_ORGANIZER = "https://aniera-festa.com"
ANIERA_PROOF = (
    "「ナガノアニエラフェスタ2026」にBanG Dream!プロジェクトより、RAISE A SUILENの出演が決定！ "
    "9月20日(日) TEMPEST STAGEにRAISE A SUILENが出演"
)
ENSEMBLE_BAND_LIVE_ID = "7f38d28a-ebb1-48f1-b50b-0d869fae0102"
ENSEMBLE_BAND_LIVE_URL = "https://www.bluenoteplace.jp/ensemble_stars_2026/"
ENSEMBLE_BAND_LIVE_SOURCE = "blue-note-place-ensemble-band-live"


def _jst(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Tokyo"))


def repair_ensemble_band_live(catalog: Catalog, *, apply: bool) -> dict:
    """Use the organizer's explicit four-day, three-session schedule for Lawson 73531."""
    with catalog.connect() as conn, conn.transaction():
        activity = conn.execute(
            "SELECT id,title,kind,official_url FROM catalog_activities WHERE id=%s FOR UPDATE",
            (ENSEMBLE_BAND_LIVE_ID,),
        ).fetchone()
        if not activity:
            raise RuntimeError("ENSEMBLE STARS!! BAND LIVE activity is missing")
        resource = conn.execute("""
            SELECT source_id,external_id,content_hash,url,content FROM allfeeds.resources
            WHERE source_id=%s AND url=%s ORDER BY observed_at DESC LIMIT 1
        """, (ENSEMBLE_BAND_LIVE_SOURCE, ENSEMBLE_BAND_LIVE_URL)).fetchone()
        if not resource:
            raise RuntimeError("Official organizer resource has not been collected")
        body = resource["content"]
        required = (
            "各日３公演", "9月13日", "9月14日", "9月15日", "9月16日",
            "13:00", "16:30", "20:00", "【一般発売（先着）】",
            "【リセール】", "【ESプレミアムパス限定先行】",
            "抽選受付期間", "当落確認/チケット入金期間",
        )
        missing = [phrase for phrase in required if phrase not in body]
        if missing:
            raise RuntimeError(f"Organizer evidence is incomplete: {missing}")
        old_occurrences = conn.execute("""
            SELECT id,starts_on FROM catalog_occurrences WHERE activity_id=%s
              AND status<>'SUPERSEDED' AND precision='DATE'
              AND starts_on BETWEEN '2026-09-13' AND '2026-09-16'
            ORDER BY starts_on
        """, (ENSEMBLE_BAND_LIVE_ID,)).fetchall()
        active_sessions = conn.execute("""
            SELECT count(*) AS n FROM catalog_occurrences WHERE activity_id=%s
              AND status<>'SUPERSEDED' AND precision='TIME'
              AND starts_at BETWEEN '2026-09-13 00:00:00+09' AND '2026-09-17 00:00:00+09'
        """, (ENSEMBLE_BAND_LIVE_ID,)).fetchone()["n"]
        result = {
            "activity_id": ENSEMBLE_BAND_LIVE_ID,
            "official_url": resource["url"],
            "official_resource": resource["external_id"],
            "sessions": [f"2026-09-{day:02d} {clock}" for day in range(13, 17)
                         for clock in ("13:00", "16:30", "20:00")],
            "date_only_occurrences_to_supersede": old_occurrences,
            "already_repaired": active_sessions == 12 and not old_occurrences,
            "applied": apply,
        }
        if not apply:
            return result
        if result["already_repaired"]:
            result["applied"] = False
            return result
        if len(old_occurrences) not in {0, 4}:
            raise RuntimeError("Unexpected partial date-only occurrence set; review manually")
        # A reviewed activity mapping prevents the official import from creating
        # a second activity whose title differs only in Unicode width.
        group_key = "official:blue-note-place:ensemble-band-live:2026"
        mapped = conn.execute(
            "SELECT activity_id FROM catalog_external_ids WHERE key=%s", (group_key,)
        ).fetchone()
        if mapped and mapped["activity_id"] != ENSEMBLE_BAND_LIVE_ID:
            raise RuntimeError("Official identity is already mapped to another activity")
        conn.execute("""INSERT INTO catalog_external_ids(key,activity_id) VALUES(%s,%s)
            ON CONFLICT(key) DO NOTHING""", (group_key, ENSEMBLE_BAND_LIVE_ID))
        proof = body[body.find("■公演チケット"):body.find("【チケット注意事項】")]
        evidence = EvidenceInput(
            source_id=resource["source_id"], external_id=resource["external_id"],
            version_hash=resource["content_hash"], url=resource["url"],
            excerpt=(body[:1100] + "\n" + proof)[:4000],
            field_path="activity.sessions_and_tickets",
            method="editorial:official-organizer-crosscheck", verified=True,
        )
        for day in range(13, 17):
            for slot, clock in enumerate(("13:00", "16:30", "20:00"), 1):
                stamp = f"2026-09-{day:02d}T{clock}:00"
                catalog.publish(ActivityInput(
                    activity_key=group_key,
                    source_key=f"official:blue-note-place:ensemble-band-live:2026-09-{day:02d}:slot-{slot}",
                    occurrence_key=f"blue-note-place:2026-09-{day:02d}:slot-{slot}",
                    occurrence_label=f"9月{day}日 · 第{slot}场（{clock}开演）",
                    title="ENSEMBLE STARS!! BAND LIVE",
                    title_zh="ENSEMBLE STARS!! BAND LIVE",
                    kind="LIVE", publication="PUBLISHED", attendance="OFFLINE",
                    subject_slugs=["ensemble-stars"],
                    time=Moment(precision="TIME", starts_at=_jst(stamp)),
                    venue="BLUE NOTE PLACE", city="东京", url=resource["url"],
                    evidence=evidence,
                ), historical=True, conn=conn)
        schedule = (
            ("premium-pass-lottery", "TICKET", "ES Premium Pass 限定先行抽选",
             "2026-06-29T00:00:00", "2026-07-20T23:59:00", "CONFIRMED"),
            ("premium-pass-result", "RESULT", "ES Premium Pass 先行抽选结果发表",
             "2026-08-06T15:00:00", None, "CONFIRMED"),
            ("premium-pass-payment", "PAYMENT", "ES Premium Pass 中选票款支付",
             "2026-08-06T15:00:00", "2026-08-11T23:00:00", "CONFIRMED"),
            ("resale", "TICKET", "官方转售受理",
             "2026-08-29T20:00:00", "2026-08-31T23:59:00", "CONFIRMED"),
            ("resale-result", "RESULT", "官方转售抽选结果发表",
             "2026-09-05T15:00:00", None, "CONFIRMED"),
            ("e-ticket-display", "UPDATE", "电子票预计开始显示",
             "2026-08-27T12:00:00", None, "REVIEW"),
        )
        for key, kind, title, starts, ends, status in schedule:
            catalog.milestone(conn, ENSEMBLE_BAND_LIVE_ID, None, MilestoneInput(
                source_key=f"official:blue-note-place:{key}", kind=kind, title=title,
                title_zh=title, status=status, round_key=key,
                time=Moment(precision="TIME", starts_at=_jst(starts),
                            ends_at=_jst(ends) if ends else None),
                url="https://l-tike.com/es-bandlive/", platform="Lawson Ticket",
                evidence=evidence, notes="主办方标注为预计时间" if status == "REVIEW" else None,
            ), historical=True)
        # Lawson's general sale already has a precise native ticket window.
        # Attach organizer evidence and make its purpose readable, rather than
        # creating a second milestone at the same opening time.
        general = conn.execute("""
            SELECT id FROM catalog_milestones WHERE activity_id=%s AND kind='TICKET'
              AND status='CONFIRMED' AND starts_at='2026-08-29 11:00:00+00'
              AND ends_at='2026-09-12 13:00:00+00' AND title='一般発売 先着'
        """, (ENSEMBLE_BAND_LIVE_ID,)).fetchone()
        if not general:
            raise RuntimeError("Expected Lawson general-sale window is missing")
        conn.execute("""UPDATE catalog_milestones SET title='一般贩售（先到先得）',
            url='https://l-tike.com/es-bandlive/',updated_at=NOW() WHERE id=%s""",
            (general["id"],))
        catalog.evidence(conn, ENSEMBLE_BAND_LIVE_ID, evidence, general["id"])
        old_ids = [row["id"] for row in old_occurrences]
        if old_ids:
            conn.execute("UPDATE catalog_occurrences SET status='SUPERSEDED' WHERE id=ANY(%s)",
                         (old_ids,))
            obsolete = conn.execute("""
                SELECT DISTINCT s.milestone_id FROM catalog_milestone_scopes s
                JOIN catalog_milestones m ON m.id=s.milestone_id
                WHERE s.occurrence_id=ANY(%s) AND m.kind='START'
                  AND m.title='活动开始' AND m.precision='DATE'
            """, (old_ids,)).fetchall()
            obsolete_ids = [row["milestone_id"] for row in obsolete]
            if obsolete_ids:
                conn.execute("UPDATE catalog_milestones SET status='SUPERSEDED',updated_at=NOW() WHERE id=ANY(%s)",
                             (obsolete_ids,))
                conn.execute("""UPDATE genchi_private.mail_queue SET status='CANCELED'
                    WHERE milestone_id=ANY(%s) AND status IN ('PENDING','SENDING')""", (obsolete_ids,))
        conn.execute("""UPDATE catalog_activities SET kind='LIVE',official_url=%s,
            title='ENSEMBLE STARS!! BAND LIVE',updated_at=NOW() WHERE id=%s""",
            (resource["url"], ENSEMBLE_BAND_LIVE_ID))
        conn.execute("""INSERT INTO catalog_changes(activity_id,kind,summary,before_value,after_value,notify)
            VALUES(%s,'DATA_REPAIRED','以主办方公告补全每天三场开演时间及票务节点',%s,%s,FALSE)""",
            (ENSEMBLE_BAND_LIVE_ID,
             Jsonb({"title": activity["title"], "kind": activity["kind"],
                    "official_url": activity["official_url"],
                    "date_only_occurrences": old_ids}),
             Jsonb({"official_url": resource["url"], "sessions": result["sessions"],
                    "official_resource": resource["external_id"]})))
        return result


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
        "published_overseas_location": """
            SELECT DISTINCT a.id,a.title,o.venue,o.city FROM catalog_activities a
            JOIN catalog_occurrences o ON o.activity_id=a.id AND o.status<>'SUPERSEDED'
            WHERE a.publication='PUBLISHED' AND concat_ws(' ',a.title,o.venue,o.city) ~*
              '(香港|Hong Kong|Seoul|서울|韓国|韩国|Korea|台北|Taipei|上海|Shanghai|北京|Beijing)'
            ORDER BY a.title LIMIT %s""",
        "generic_start_milestone": """
            SELECT m.activity_id,a.title,m.id AS milestone_id,m.starts_on,m.starts_at
            FROM catalog_milestones m JOIN catalog_activities a ON a.id=m.activity_id
            WHERE a.publication='PUBLISHED' AND m.status='CONFIRMED'
              AND m.kind='START' AND m.title='活动开始'
            ORDER BY a.updated_at DESC LIMIT %s""",
        "lawson_native_sessions_collapsed": """
            WITH parsed AS (
              SELECT r.id,r.title,
                (SELECT count(DISTINCT e->>'startsAt')
                 FROM jsonb_array_elements(r.attributes->'ticket_page'->'events') e
                 WHERE e ? 'startsAt') AS parsed_dates,
                (SELECT count(DISTINCT key)
                 FROM jsonb_array_elements(r.attributes->'ticket_page'->'events') e
                 CROSS JOIN LATERAL jsonb_array_elements(e->'ticketWindows') w
                 CROSS JOIN LATERAL jsonb_array_elements_text(
                   CASE WHEN jsonb_typeof(w->'nativePerformanceKeys')='array'
                     THEN w->'nativePerformanceKeys' ELSE '[]'::jsonb END
                 ) key
                 WHERE key <> '') AS native_keys
              FROM allfeeds.resources r
              WHERE r.attributes->>'source_type'='lawson_ticket'
            )
            SELECT id,title,parsed_dates,native_keys FROM parsed
            WHERE native_keys>parsed_dates
            ORDER BY native_keys DESC LIMIT %s""",
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


def official_link_candidates(conn) -> list[dict]:
    """Exact official-title matches whose complete extracted date set already exists."""
    return conn.execute("""
        WITH candidates AS (
          SELECT r.url AS source_url,r.external_id,r.content_hash,r.content,
            rv.payload->'activity'->>'title' AS title,
            COALESCE((rv.payload->'activity'->'time'->>'starts_on')::date,
              ((rv.payload->'activity'->'time'->>'starts_at')::timestamptz
                AT TIME ZONE 'Asia/Tokyo')::date) AS starts_on,
            COALESCE((rv.payload->'activity'->'time'->>'ends_on')::date,
              ((rv.payload->'activity'->'time'->>'ends_at')::timestamptz
                AT TIME ZONE 'Asia/Tokyo')::date) AS ends_on
          FROM catalog_reviews rv JOIN allfeeds.resources r ON r.id=rv.resource_id
          WHERE r.source_id='bang-dream-events' AND rv.status='PENDING'
        ), matches AS (
          SELECT a.id,a.title,a.official_url,c.source_url,c.external_id,
            c.content_hash,c.content,c.starts_on,c.ends_on,
            EXISTS(SELECT 1 FROM catalog_occurrences o
              WHERE o.activity_id=a.id AND o.status<>'SUPERSEDED'
              AND c.starts_on BETWEEN
                COALESCE(o.starts_on,(o.starts_at AT TIME ZONE 'Asia/Tokyo')::date)
                AND COALESCE(o.ends_on,(o.ends_at AT TIME ZONE 'Asia/Tokyo')::date,
                  o.starts_on,(o.starts_at AT TIME ZONE 'Asia/Tokyo')::date))
            AND (c.ends_on IS NULL OR EXISTS(SELECT 1 FROM catalog_occurrences o
              WHERE o.activity_id=a.id AND o.status<>'SUPERSEDED'
              AND c.ends_on BETWEEN
                COALESCE(o.starts_on,(o.starts_at AT TIME ZONE 'Asia/Tokyo')::date)
                AND COALESCE(o.ends_on,(o.ends_at AT TIME ZONE 'Asia/Tokyo')::date,
                  o.starts_on,(o.starts_at AT TIME ZONE 'Asia/Tokyo')::date))) AS day_matches
          FROM candidates c JOIN catalog_activities a
            ON a.title=c.title AND a.publication='PUBLISHED'
          WHERE a.official_url ~* '^https?://([^/]+\\.)?bandori\\.fans/'
            AND c.starts_on IS NOT NULL
            AND c.content !~* '(香港|Hong Kong|Seoul|서울|韓国|韩国|Korea|台北|Taipei|上海|Shanghai|北京|Beijing)'
            AND NOT EXISTS(SELECT 1 FROM catalog_occurrences foreign_occurrence
              WHERE foreign_occurrence.activity_id=a.id AND foreign_occurrence.status<>'SUPERSEDED'
              AND concat_ws(' ',foreign_occurrence.venue,foreign_occurrence.city) ~*
                '(香港|Hong Kong|Seoul|서울|韓国|韩国|Korea|台北|Taipei|上海|Shanghai|北京|Beijing)')
        )
        SELECT id,title,official_url AS previous_url,max(source_url) AS source_url,
          max(external_id) AS external_id,max(content_hash) AS content_hash,
          left(max(content),4000) AS excerpt,
          array_agg(DISTINCT (starts_on::text || COALESCE('..' || ends_on::text,''))
            ORDER BY (starts_on::text || COALESCE('..' || ends_on::text,''))) AS dates
        FROM matches GROUP BY id,title,official_url
        HAVING bool_and(day_matches)
          AND count(DISTINCT source_url)=1
        ORDER BY title
    """).fetchall()


def repair_community_links(catalog: Catalog, *, apply: bool) -> dict:
    """Replace a community canonical URL only after an exact official title/date match."""
    with catalog.connect() as conn, conn.transaction():
        candidates = official_link_candidates(conn)
        result = {"count": len(candidates), "candidates": candidates, "applied": apply}
        if not apply:
            return result
        for candidate in candidates:
            evidence_id = fingerprint(
                f"{candidate['id']}|official-link|{candidate['source_url']}|{candidate['content_hash']}"
            )
            conn.execute("""
                INSERT INTO catalog_evidence(id,activity_id,source_id,external_id,version_hash,
                  url,excerpt,field_path,method,verified)
                VALUES(%s,%s,'bang-dream-events',%s,%s,%s,%s,'official_url',
                  'rule:official-exact-title-date',TRUE)
                ON CONFLICT(id) DO UPDATE SET verified=TRUE,observed_at=NOW()
            """, (evidence_id,candidate["id"],candidate["external_id"],candidate["content_hash"],
                    candidate["source_url"],candidate["excerpt"]))
            conn.execute("UPDATE catalog_activities SET official_url=%s,updated_at=NOW() WHERE id=%s",
                         (candidate["source_url"],candidate["id"]))
            conn.execute("""INSERT INTO catalog_changes(
                activity_id,kind,summary,before_value,after_value,notify)
                VALUES(%s,'DATA_REPAIRED','以标题及完整日期集合匹配的官方活动页替换社区链接',%s,%s,FALSE)""",
                (candidate["id"],Jsonb({"official_url":candidate["previous_url"]}),
                 Jsonb({"official_url":candidate["source_url"],"dates":candidate["dates"],
                        "evidence_id":evidence_id})))
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
    links_parser = sub.add_parser("repair-community-links")
    links_parser.add_argument("--apply", action="store_true")
    ensemble_parser = sub.add_parser("repair-ensemble-band-live")
    ensemble_parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    catalog = Catalog()
    if args.command == "audit":
        result = audit(catalog, args.limit)
    elif args.command == "repair-aniera":
        result = repair_aniera(catalog, apply=args.apply)
    elif args.command == "repair-ensemble-band-live":
        result = repair_ensemble_band_live(catalog, apply=args.apply)
    else:
        result = repair_community_links(catalog, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
