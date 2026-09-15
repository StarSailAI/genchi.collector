"""Repair proven venue aliases and quarantine non-occurrence ticket products."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC

from .domain import Moment, fingerprint
from .naming import sync_name
from .pipeline import structured
from .schedule_quality import TIME_FIELDS
from .store import Catalog, json_value
from .venues import bundle_venue, nonphysical_venue, venue_key


def repair(catalog: Catalog, *, apply=False):
    plans = []
    with catalog.connect() as conn, conn.transaction():
        rows = conn.execute("""SELECT o.*,a.title activity_title,
            (SELECT min(m.created_at) FROM catalog_milestone_scopes s JOIN catalog_milestones m ON m.id=s.milestone_id WHERE s.occurrence_id=o.id) first_seen
            FROM catalog_occurrences o
            JOIN catalog_activities a ON a.id=o.activity_id WHERE o.status<>'SUPERSEDED'
            AND a.publication='PUBLISHED' ORDER BY first_seen,o.id FOR UPDATE OF o""").fetchall()
        groups = defaultdict(list)
        for row in rows:
            if nonphysical_venue(row["venue"]) or bundle_venue(row["venue"]):
                plans.append(
                    {
                        "action": "review_nonphysical_or_bundle",
                        "occurrence_id": row["id"],
                        "activity_id": row["activity_id"],
                        "venue": row["venue"],
                    }
                )
                if apply:
                    conn.execute(
                        "UPDATE catalog_occurrences SET status='SUPERSEDED' WHERE id=%s",
                        (row["id"],),
                    )
                    catalog.review(
                        conn,
                        key="non-occurrence:" + row["id"],
                        activity_id=row["activity_id"],
                        reason="线上平台或多场通票被写成实体会场；原文保留，票务适用范围需核对",
                        payload=plans[-1],
                    )
                    orphaned = conn.execute(
                        """SELECT m.id FROM catalog_milestones m JOIN catalog_milestone_scopes s ON s.milestone_id=m.id
                        WHERE s.occurrence_id=%s AND m.status<>'SUPERSEDED' AND NOT EXISTS(
                          SELECT 1 FROM catalog_milestone_scopes s2 JOIN catalog_occurrences o2 ON o2.id=s2.occurrence_id
                          WHERE s2.milestone_id=m.id AND o2.status<>'SUPERSEDED')""",
                        (row["id"],),
                    ).fetchall()
                    ids = [m["id"] for m in orphaned]
                    conn.execute(
                        "UPDATE catalog_milestones SET status=CASE WHEN kind IN ('START','DOORS','PERIOD') THEN 'SUPERSEDED' ELSE 'REVIEW' END,updated_at=NOW() WHERE id=ANY(%s)",
                        (ids,),
                    )
                    conn.execute(
                        "UPDATE genchi_private.mail_queue SET status='CANCELED' WHERE milestone_id=ANY(%s) AND status IN ('PENDING','SENDING')",
                        (ids,),
                    )
                    catalog.change(
                        conn,
                        row["activity_id"],
                        None,
                        "DATA_REPAIRED",
                        "撤下误作线下场次的线上平台或通票条目",
                        row,
                        plans[-1],
                        True,
                    )
                continue
            time = Moment(**{k: row[k] for k in TIME_FIELDS})
            if time.precision != "TIME":
                continue
            key = venue_key(row["venue"], row["activity_title"], time.anchor()[:4])
            if not key.startswith("venue:"):
                continue
            groups[
                (row["activity_id"], key, *(str(row[k]) for k in TIME_FIELDS), row["status"])
            ].append(row)
        for (_, key, *_), members in groups.items():
            keep = members[0]
            time = Moment(**{k: keep[k] for k in TIME_FIELDS})
            clock = time.starts_at.astimezone(UTC).isoformat() if time.starts_at else time.anchor()
            identity = fingerprint(
                f"{clock}:{key}"
                + (":period" if time.ends_on and time.ends_on != time.starts_on else "")
            )
            # Different end boundaries remain separate facts requiring review, not an alias merge.
            owner = conn.execute(
                "SELECT id FROM catalog_occurrences WHERE activity_id=%s AND identity_key=%s",
                (keep["activity_id"], identity),
            ).fetchone()
            if owner and owner["id"] not in {m["id"] for m in members}:
                continue
            for old in members:
                if old["id"] == keep["id"]:
                    continue
                plan = {
                    "action": "merge_verified_alias",
                    "activity_id": keep["activity_id"],
                    "from": old["id"],
                    "to": keep["id"],
                    "from_venue": old["venue"],
                    "to_venue": keep["venue"],
                }
                plans.append(plan)
                if not apply:
                    continue
                conn.execute(
                    "UPDATE catalog_external_ids SET occurrence_id=%s WHERE occurrence_id=%s",
                    (keep["id"], old["id"]),
                )
                conn.execute(
                    "INSERT INTO catalog_milestone_scopes SELECT milestone_id,%s FROM catalog_milestone_scopes WHERE occurrence_id=%s ON CONFLICT DO NOTHING",
                    (keep["id"], old["id"]),
                )
                conn.execute(
                    "DELETE FROM catalog_milestone_scopes WHERE occurrence_id=%s", (old["id"],)
                )
                conn.execute(
                    "UPDATE catalog_occurrences SET status='SUPERSEDED',identity_key=%s WHERE id=%s", (fingerprint("superseded:" + old["id"] + ":" + old["identity_key"]), old["id"])
                )
                catalog.change(
                    conn,
                    keep["activity_id"],
                    None,
                    "DATA_REPAIRED",
                    "合并已核实的同会场同时间重复安排，保留来源及旧 ID",
                    old,
                    plan,
                    True,
                )
            if apply:
                conn.execute(
                    "UPDATE catalog_occurrences SET identity_key=%s WHERE id=%s",
                    (identity, keep["id"]),
                )
                nodes = conn.execute(
                    """SELECT m.* FROM catalog_milestones m JOIN catalog_milestone_scopes s ON s.milestone_id=m.id
                    WHERE s.occurrence_id=%s AND m.status='CONFIRMED' AND m.details->>'generated_from_occurrence'='true'
                    AND NOT EXISTS(SELECT 1 FROM catalog_milestone_scopes s2 WHERE s2.milestone_id=m.id AND s2.occurrence_id<>%s)
                    ORDER BY m.created_at,m.id""",
                    (keep["id"], keep["id"]),
                ).fetchall()
                seen = {}
                for node in nodes:
                    fact = (
                        node["kind"],
                        *(str(node[k]) for k in TIME_FIELDS),
                        node["details"].get("schedule_role"),
                    )
                    target = seen.setdefault(fact, node["id"])
                    if target == node["id"]:
                        continue
                    conn.execute(
                        "UPDATE catalog_external_ids SET milestone_id=%s WHERE milestone_id=%s",
                        (target, node["id"]),
                    )
                    conn.execute(
                        "UPDATE catalog_evidence SET milestone_id=%s WHERE milestone_id=%s",
                        (target, node["id"]),
                    )
                    conn.execute(
                        "UPDATE catalog_milestones SET status='SUPERSEDED',updated_at=NOW() WHERE id=%s",
                        (node["id"],),
                    )
                    conn.execute(
                        "UPDATE genchi_private.mail_queue SET status='CANCELED' WHERE milestone_id=%s AND status IN ('PENDING','SENDING')",
                        (node["id"],),
                    )
        if apply:
            for aid in {
                r["activity_id"] for r in plans if r["action"] == "review_nonphysical_or_bundle"
            }:
                if not conn.execute(
                    "SELECT 1 FROM catalog_occurrences WHERE activity_id=%s AND status<>'SUPERSEDED'",
                    (aid,),
                ).fetchone():
                    conn.execute(
                        "UPDATE catalog_activities SET publication='REVIEW',updated_at=NOW() WHERE id=%s",
                        (aid,),
                    )
    return {"applied": apply, "count": len(plans), "plans": plans}


def enrich_native_labels(catalog: Catalog, *, apply=False):
    changes = []
    with catalog.connect() as conn, conn.transaction():
        subjects = conn.execute("SELECT * FROM catalog_subjects").fetchall()
        resources = conn.execute(
            "SELECT * FROM allfeeds.resources WHERE attributes->'ticket_page'->>'scheduleCompleteness'='native_detail'"
        ).fetchall()
        for resource in resources:
            try:
                items = structured(resource, subjects)
            except ValueError:
                continue
            for item in items:
                if not item.occurrence_label:
                    continue
                mapped = conn.execute(
                    "SELECT * FROM catalog_external_ids WHERE key=%s", (item.source_key,)
                ).fetchone()
                if not mapped or not mapped["occurrence_id"]:
                    continue
                o = conn.execute(
                    "SELECT * FROM catalog_occurrences WHERE id=%s AND status<>'SUPERSEDED'",
                    (mapped["occurrence_id"],),
                ).fetchone()
                if not o or o["label"] == item.occurrence_label:
                    continue
                changes.append({"id": o["id"], "label": item.occurrence_label})
                if not apply:
                    continue
                conn.execute(
                    "UPDATE catalog_occurrences SET label=%s WHERE id=%s",
                    (item.occurrence_label, o["id"]),
                )
                sync_name(conn, "OCCURRENCE", o["id"])
                # Only generated schedule nodes; keep reviewed custom workflow labels.
                nodes = conn.execute(
                    """SELECT m.* FROM catalog_milestones m JOIN catalog_milestone_scopes s ON s.milestone_id=m.id
                    WHERE s.occurrence_id=%s AND m.status='CONFIRMED' AND m.details->>'generated_from_occurrence'='true'""",
                    (o["id"],),
                ).fetchall()
                from .schedules import schedule_node

                for m in nodes:
                    _, title, _ = schedule_node(
                        item.title,
                        item.kind,
                        item.time,
                        item.venue,
                        item.occurrence_label,
                        item.occurrence_role,
                    )
                    conn.execute(
                        "UPDATE catalog_milestones SET title=%s,updated_at=NOW() WHERE id=%s",
                        (title, m["id"]),
                    )
                    sync_name(conn, "MILESTONE", m["id"], approved=title)
                for node in item.milestones:
                    if node.eligibility:
                        conn.execute(
                            """UPDATE catalog_milestones SET eligibility=%s WHERE id=(SELECT milestone_id
                            FROM catalog_external_ids WHERE key=%s) AND eligibility IS NULL""",
                            (node.eligibility, mapped["activity_id"] + ":" + node.source_key),
                        )
                catalog.change(
                    conn,
                    mapped["activity_id"],
                    None,
                    "DATA_REPAIRED",
                    "补充原生票种与入场限制，保留日期和场次 ID",
                    {"label": o["label"]},
                    changes[-1],
                    True,
                )
    return {"applied": apply, "count": len(changes), "changes": changes}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["identity", "labels"])
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    result = (repair if args.command == "identity" else enrich_native_labels)(
        Catalog(), apply=args.apply
    )
    print(json.dumps(json_value(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
