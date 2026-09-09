#!/usr/bin/env python3
"""Preserve existing milestone IDs when adopting Lawson's native reception keys.

Run inside the product image after fresh Lawson collection, with its normalizer
paused. Preview is the default. Back up the database before --apply. This does
not publish candidates or remove resources, evidence, participation or history.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from genchi_product.pipeline import structured
from genchi_product.store import Catalog


def repair(conn, resources, subjects, *, apply=False):
    groups = defaultdict(dict)
    for resource in resources:
        items = structured(resource, subjects)
        windows = {window["id"]: window for event in resource["attributes"]["ticket_page"]["events"] for window in event["ticketWindows"]}
        mappings = {
            item.source_key: conn.execute(
                "SELECT * FROM catalog_external_ids WHERE key=%s", (item.source_key,),
            ).fetchone()
            for item in items if item.publication == "PUBLISHED"
        }
        activity_ids = {row["activity_id"] for row in mappings.values() if row}
        for item in items:
            if item.publication != "PUBLISHED":
                continue
            mapped = mappings.get(item.source_key)
            activity_id = mapped["activity_id"] if mapped else next(iter(activity_ids)) if len(activity_ids) == 1 else None
            if not activity_id:
                continue  # New activities have no legacy mappings to preserve.
            for node in item.milestones:
                if node.kind != "TICKET" or node.platform != "lawson":
                    continue
                window = windows[node.round_key.removeprefix("lawson:")]
                legacy = window.get("legacyId")
                if not legacy or legacy == window.get("id"):
                    continue
                old_key = f"{activity_id}:native-ticket:lawson:{legacy}"
                new_key = f"{activity_id}:{node.source_key}"
                variant = groups[old_key].setdefault(new_key, {"node": node, "scopes": set()})
                if variant["node"].time != node.time:
                    raise ValueError(f"Conflicting times for native reception {new_key}")
                if mapped and mapped["occurrence_id"]:
                    variant["scopes"].add(mapped["occurrence_id"])
    plans = []
    for old_key, variants in groups.items():
        old = conn.execute(
            "SELECT m.* FROM catalog_external_ids e JOIN catalog_milestones m ON m.id=e.milestone_id WHERE e.key=%s FOR UPDATE OF m",
            (old_key,),
        ).fetchone()
        if not old:
            continue
        choices = list(variants.items())
        if len(choices) > 1:
            choices = [(key, value) for key, value in choices if all(
                old[field] == expected for field, expected in value["node"].time.model_dump().items()
            ) and old["notes"] == value["node"].notes]
        if len(choices) != 1:
            raise ValueError(f"Cannot safely identify the existing round: {old_key}")
        new_key, chosen = choices[0]
        if old["round_key"] != chosen["node"].round_key and conn.execute(
            "SELECT 1 FROM genchi_private.participation WHERE activity_id=%s AND round_key=%s LIMIT 1",
            (old["activity_id"], old["round_key"]),
        ).fetchone():
            raise ValueError(f"Existing user participation needs a reviewed round migration: {old_key}")
        existing = conn.execute("SELECT milestone_id FROM catalog_external_ids WHERE key=%s", (new_key,)).fetchone()
        if existing and existing["milestone_id"] != old["id"]:
            raise ValueError(f"Native round already maps elsewhere: {new_key}")
        scopes = {row["occurrence_id"] for row in conn.execute(
            "SELECT occurrence_id FROM catalog_milestone_scopes WHERE milestone_id=%s", (old["id"],),
        ).fetchall()}
        # Only a proven split narrows scopes. An ordinary ID alias must not
        # discard older occurrences absent from today's bounded search results.
        removed = sorted(scopes - chosen["scopes"]) if len(variants) > 1 else []
        if existing and not removed and old["round_key"] == chosen["node"].round_key:
            continue
        plans.append({"old_key": old_key, "new_key": new_key, "milestone_id": old["id"],
                      "activity_id": old["activity_id"], "new_round_key": chosen["node"].round_key,
                      "variants": len(variants), "removed_scopes": removed})
    if apply:
        for plan in plans:
            conn.execute(
                "INSERT INTO catalog_external_ids(key,activity_id,milestone_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (plan["new_key"], plan["activity_id"], plan["milestone_id"]),
            )
            # This internal identifier repair is not a changed official deadline
            # and must not create a new revision or notification by itself.
            conn.execute("UPDATE catalog_milestones SET round_key=%s WHERE id=%s", (plan["new_round_key"], plan["milestone_id"]))
            for occurrence in plan["removed_scopes"]:
                conn.execute("DELETE FROM catalog_milestone_scopes WHERE milestone_id=%s AND occurrence_id=%s", (plan["milestone_id"], occurrence))
            Catalog.change(conn, plan["activity_id"], plan["milestone_id"], "MILESTONE_CHANGED",
                           "修正 Lawson 原生受付映射，保留节点 ID 与历史；分离不同票种的适用场次",
                           {"key": plan["old_key"], "removed_scopes": plan["removed_scopes"]},
                           {"key": plan["new_key"]}, True)
    return plans


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--observed-since", required=True, help="UTC time when the fresh Lawson task started")
    args = parser.parse_args()
    with Catalog().connect() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('lawson-round-repair',0))")
        resources = conn.execute(
            "SELECT * FROM allfeeds.resources WHERE source_id='lawson-anime-tickets' AND observed_at >= %s ORDER BY id",
            (args.observed_since,),
        ).fetchall()
        if not resources or any(not window.get("nativeReceptionKey") for resource in resources
                                for event in resource["attributes"]["ticket_page"]["events"] for window in event["ticketWindows"]):
            raise ValueError("Fresh Lawson records with native reception keys are required")
        plans = repair(conn, resources, conn.execute("SELECT * FROM catalog_subjects").fetchall(), apply=args.apply)
        print(json.dumps({"applied": args.apply, "resources": len(resources), "plans": plans}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
