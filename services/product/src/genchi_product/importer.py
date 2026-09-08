"""Non-destructive, replayable bridge from legacy records to the canonical catalog."""

from __future__ import annotations

import json
import re
from collections import defaultdict

from .domain import ActivityInput, EvidenceInput, MilestoneInput, classify, legacy_time, normalize
from .store import Catalog

PHASE_LABELS = {
    "FC_PRE": "FC 先行抽选",
    "LOTTERY_1": "一次抽选",
    "LOTTERY_2": "二次抽选",
    "LOTTERY_3": "三次抽选",
    "ADVANCE": "先行发售",
    "GENERAL": "一般发售",
    "DAY_OF": "当日券",
    "RESALE": "再贩",
    "OTHER": "售票",
}


def subjects_for(title: str, subjects: list[dict], fallback: str | None = None) -> list[str]:
    text = normalize(title)
    matches = []
    for subject in subjects:
        aliases = [subject["name"], subject.get("name_zh") or "", *(subject.get("aliases") or [])]
        if any(len(normalize(alias)) >= 3 and normalize(alias) in text for alias in aliases):
            matches.append(subject["slug"])
    if fallback and any(s["slug"] == fallback for s in subjects):
        matches.append(fallback)
    return sorted(set(matches))


def import_legacy(catalog: Catalog) -> dict:
    with catalog.connect() as conn:
        subjects = conn.execute("SELECT * FROM catalog_subjects").fetchall()
        events = conn.execute("""SELECT e.*,v."nameJa" AS venue_name,v.city,s.key AS source_key,
            ip.slug AS subject_slug,candidate."contentItemId" AS content_id,
            ci."titleOriginal" AS original_title,ci."bodyOriginal" AS original_body,
            ci."externalId" AS external_id,ci."rawContentHash" AS content_hash,ci."publishedAt" AS published_at,
            a."reviewStatus" AS activity_review
            FROM "Event" e LEFT JOIN "Venue" v ON v.id=e."venueId"
            LEFT JOIN "Source" s ON s.id=e."sourceId" LEFT JOIN "Ip" ip ON ip.id=e."ipId"
            LEFT JOIN "ActivityProfile" a ON a.id=e."activityId"
            LEFT JOIN LATERAL (SELECT * FROM "ExtractionCandidate" c WHERE c."appliedEntityId"=e.id
                ORDER BY c."updatedAt" DESC LIMIT 1) candidate ON TRUE
            LEFT JOIN "ContentItem" ci ON ci.id=candidate."contentItemId" ORDER BY e."createdAt",e.id""").fetchall()
        tickets = defaultdict(list)
        for row in conn.execute('SELECT * FROM "TicketWindow"').fetchall():
            tickets[row["eventId"]].append(row)
        ids = set()
        rejected = 0
        for row in events:
            title = row["titleJa"] or row["titleZh"] or "未命名活动"
            native = (row.get("sourceKey") or "").startswith(
                ("asobi:", "eplus:", "pia:", "lawson:")
            )
            evidence = EvidenceInput(
                source_id=row["source_key"],
                external_id=row["external_id"] or row["sourceKey"],
                version_hash=row["content_hash"],
                url=row["officialUrl"],
                method="legacy-import",
                excerpt=(
                    row["original_body"]
                    or json.dumps(
                        {
                            "title": title,
                            "startsAt": str(row["startsAt"]),
                            "sourceKey": row["sourceKey"],
                        },
                        ensure_ascii=False,
                    )
                )[:4000],
                verified=False,
                published_at=row["published_at"],
            )
            node_list = []
            for ticket in tickets[row["id"]]:
                label = (
                    ticket["phaseLabelZh"]
                    or ticket["phaseLabelJa"]
                    or PHASE_LABELS[ticket["phase"]]
                )
                round_key = (
                    f"{ticket['platform']}:{label}:{ticket.get('url')}:{ticket['opensAt'].date()}"
                )
                node_evidence = evidence.model_copy(
                    update={
                        "field_path": "ticket",
                        "url": ticket["url"] or evidence.url,
                        "excerpt": json.dumps(
                            {
                                "round": label,
                                "opensAt": str(ticket["opensAt"]),
                                "closesAt": str(ticket["closesAt"]),
                                "resultAt": str(ticket["resultAt"]),
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
                node_list.append(
                    MilestoneInput(
                        source_key=f"legacy-ticket:{ticket['id']}",
                        kind="TICKET",
                        title=label,
                        time=legacy_time(ticket["opensAt"], ticket["closesAt"]),
                        platform=ticket["platform"],
                        round_key=round_key,
                        url=ticket["url"],
                        notes=ticket["notes"],
                        status="CANCELED" if ticket["status"] == "CANCELED" else "CONFIRMED",
                        details={"phase": ticket["phase"], "price_jpy": ticket["priceJpy"]},
                        evidence=node_evidence,
                    )
                )
                if ticket["resultAt"]:
                    node_list.append(
                        MilestoneInput(
                            source_key=f"legacy-result:{ticket['id']}",
                            kind="RESULT",
                            title=f"{label} · 结果公布",
                            time=legacy_time(ticket["resultAt"]),
                            platform=ticket["platform"],
                            round_key=round_key,
                            url=ticket["url"],
                            requires="APPLIED",
                            evidence=node_evidence,
                        )
                    )
            if row["doorsAt"]:
                node_list.append(
                    MilestoneInput(
                        source_key=f"legacy-doors:{row['id']}",
                        kind="DOORS",
                        title="开放入场",
                        time=legacy_time(row["doorsAt"]),
                        url=row["officialUrl"],
                        evidence=evidence,
                    )
                )
            item = ActivityInput(
                source_key=f"native:{row['sourceKey']}" if native else f"legacy:{row['id']}",
                title=title,
                title_zh=row["titleZh"],
                kind=classify(title, "OTHER" if row["eventType"] == "LIVE" else row["eventType"]),
                url=row["officialUrl"],
                subject_slugs=subjects_for(title, subjects, row["subject_slug"]),
                occurrence_key=row["sourceKey"] or row["id"],
                time=legacy_time(row["startsAt"], row["endsAt"]),
                venue=row["venue_name"],
                city=row["city"],
                status=row["status"] if row["status"] in {"CANCELED", "POSTPONED"} else "SCHEDULED",
                publication="REJECTED" if row["activity_review"] == "REJECTED" else "PUBLISHED",
                evidence=evidence,
                milestones=node_list,
            )
            if item.attendance == "ONLINE":
                item.publication = "REJECTED"
                rejected += 1
            with conn.transaction():
                activity_id = catalog.publish(item, historical=True, conn=conn)
                conn.execute(
                    "INSERT INTO catalog_external_ids(key,activity_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (f"legacy-slug:{row['slug']}", activity_id),
                )
            # Connect the same native ticket to its imported node before structured verification.
            for ticket in tickets[row["id"]]:
                native_ticket = ticket.get("sourceKey") or ""
                match = re.match(r"(asobi|eplus|pia|lawson):reception:(.+)", native_ticket)
                if not match:
                    continue
                platform, suffix = match.groups()
                if platform == "asobi":
                    round_id = suffix.split(":act:")[0]
                else:
                    parts = suffix.split(":")
                    if len(parts) < 2:
                        continue
                    round_id = parts[1]
                round_key = f"{platform}:{round_id}"
                for prefix, legacy_prefix in (
                    ("native-ticket", "legacy-ticket"),
                    ("native-RESULT", "legacy-result"),
                ):
                    conn.execute(
                        """INSERT INTO catalog_external_ids(key,activity_id,milestone_id)
                        SELECT %s,activity_id,milestone_id FROM catalog_external_ids WHERE key=%s
                        ON CONFLICT DO NOTHING""",
                        (
                            f"{activity_id}:{prefix}:{round_key}",
                            f"{activity_id}:{legacy_prefix}:{ticket['id']}",
                        ),
                    )
            ids.add(activity_id)
        # Current inputs are already represented by the historical bridge. Only future revisions enqueue new work.
        conn.execute("""INSERT INTO catalog_jobs(resource_id,content_hash,status)
            SELECT id,content_hash,'IMPORTED' FROM allfeeds.resources ON CONFLICT DO NOTHING""")
        conn.commit()
        return {
            "legacy_rows": len(events),
            "canonical_activities": len(ids),
            "online_rows_excluded": rejected,
        }
