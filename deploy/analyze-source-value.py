#!/usr/bin/env python3
"""Read-only, version-scoped source coverage report. Run inside remote product."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime

from genchi_product.domain import JST, ActivityInput, normalize
from genchi_product.matching import event_reference
from genchi_product.pipeline import structured
from genchi_product.store import Catalog


def analyze(resources, candidates, evidence, subjects=()):
    by_source = defaultdict(list)
    by_resource = defaultdict(list)
    for candidate in candidates:
        try:
            item = ActivityInput.model_validate(candidate["payload"]["activity"])
        except (KeyError, ValueError, TypeError):
            continue
        by_resource[candidate["resource_id"]].append(item)
    clusters = defaultdict(lambda: defaultdict(list))
    bodies = defaultdict(set)
    native_resources = set()
    native_failures = Counter()
    for resource in resources:
        if (resource.get("attributes") or {}).get("source_type") in {"asobi_ticket", "eplus_ticket", "pia_ticket", "lawson_ticket"}:
            try:
                items = structured(resource, list(subjects))
            except ValueError:
                native_failures[resource["source_id"]] += 1
                continue
            if items:
                # Reconstruct deterministic native facts without any LLM call or writes.
                by_resource[resource["id"]] = items
                native_resources.add(resource["id"])
    output = {}
    for resource in resources:
        by_source[resource["source_id"]].append(resource)
        if resource.get("content"):
            bodies[hashlib.sha256(normalize(resource["content"]).encode()).hexdigest()].add(resource["source_id"])
    for source, rows in by_source.items():
        kinds, nodes, dates = Counter(), Counter(), Counter()
        relevant, unknown_dates, references, candidate_count, native_count = 0, 0, 0, 0, 0
        ended = 0
        for row in rows:
            attributes = row.get("attributes") or {}
            dates[attributes.get("published_precision") or ("UNSPECIFIED" if row.get("published_at") else "TBD")] += 1
            items = by_resource[row["id"]]
            relevant += bool(items)
            for item in items:
                if row["id"] in native_resources:
                    native_count += 1
                else:
                    candidate_count += 1
                kinds[item.kind] += 1
                nodes.update(n.kind for n in item.milestones)
                unknown_dates += item.time.precision == "TBD"
                reference = event_reference(item.url)
                references += bool(reference)
                ended += item.time.anchor() != "TBD" and str(item.time.ends_on or (item.time.ends_at.astimezone(JST).date() if item.time.ends_at else item.time.anchor())) < datetime.now(JST).date().isoformat()
                # These are comparison groups, NOT automatic identity decisions.
                # No translated titles, no transitive fuzzy clustering.
                links = {ref for url in [item.url, *[n.url for n in item.milestones]] if (ref := event_reference(url))}
                for link in links if item.time.precision != "TBD" else []:
                    # Kind classifications differ between native tickets and news;
                    # retain them for human comparison, not as a join prerequisite.
                    key = (link, item.time.anchor())
                    clusters[key][source].append({"url": row["url"], "activity_title": item.title,
                                                  "kind": item.kind, "time": item.time.model_dump(mode="json"),
                                                  "fact_origin": "native_ticket" if row["id"] in native_resources else "unreviewed_candidate",
                                                  "published_at": row.get("published_at"),
                                                  "published_precision": attributes.get("published_precision", "UNSPECIFIED"),
                                                  "milestones": [{"kind": n.kind, "title": n.title, "time": n.time.model_dump(mode="json"), "url": n.url} for n in item.milestones]})
        published = [r["published_at"] for r in rows if r.get("published_at")]
        output[source] = {
            "observed_resources": len(rows), "job_status": dict(Counter(r["job_status"] for r in rows)),
            "publication_date_precision": dict(dates),
            "oldest_publication": min(published) if published else None,
            "newest_publication": max(published) if published else None,
            "resources_with_activity_facts": relevant, "activity_candidates": candidate_count,
            "native_ticket_activity_facts": native_count, "native_parse_failures": native_failures[source],
            "activity_kinds": dict(kinds), "milestone_kinds": dict(nodes),
            "facts_without_event_date": unknown_dates, "facts_with_specific_event_link": references,
            "facts_ended_before_report_date": ended,
            "resources_with_image_details_pending": sum(bool((r.get("attributes") or {}).get("image_details_pending")) for r in rows),
            "original_publishers": dict(Counter((r.get("attributes") or {}).get("original_publisher", {}).get("label", "UNSPECIFIED") for r in rows)),
            "exact_body_shared_resources": sum(bool(bodies[hashlib.sha256(normalize(r.get("content") or "").encode()).hexdigest()] - {source}) for r in rows if r.get("content")),
            "published_activity_ids": len({e["activity_id"] for e in evidence if e["source_id"] == source}),
        }
    comparable = [{"reference": key[0], "date": key[1], "sources": sources}
                  for key, sources in clusters.items() if len(sources) > 1]
    return {
        "sources": output, "cross_source_comparison_groups": comparable,
        "limitations": [
            "Counts describe this bounded observation sample, not total website coverage or true recall.",
            "Candidates remain unreviewed; extraction failure and irrelevant content are different outcomes.",
            "Native ticket facts are reconstructed deterministically, separate from model candidates and publication counts.",
            "No shared group is not proof of unique coverage: official links/names may differ or candidates may be absent.",
            "Publication, modification, first observation and event dates are different clocks. DATE is not a precise timestamp.",
            "Same activity publications may describe different updates; compare the same milestone before claiming source latency.",
            "Do not disable a source solely because it has fewer records or no matches in this sample.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True)
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    if since.tzinfo is None:
        parser.error("--since requires an explicit timezone")
    with Catalog().connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        resources = conn.execute("""SELECT r.*,j.status job_status FROM allfeeds.resources r
            LEFT JOIN genchi.catalog_jobs j ON j.resource_id=r.id AND j.content_hash=r.content_hash
            WHERE r.observed_at>=%s ORDER BY r.source_id,r.id""", (since,)).fetchall()
        candidates = conn.execute("""SELECT v.resource_id,v.payload FROM catalog_reviews v
            JOIN allfeeds.resources r ON r.id=v.resource_id
            WHERE r.observed_at>=%s AND v.payload ? 'activity'
            AND v.payload->'activity'->'evidence'->>'version_hash'=r.content_hash
            AND v.status <> 'REJECTED'""", (since,)).fetchall()
        evidence = conn.execute("""SELECT DISTINCT e.source_id,e.activity_id FROM catalog_evidence e
            JOIN allfeeds.resources r ON r.source_id=e.source_id AND r.external_id=e.external_id
            AND r.content_hash=e.version_hash JOIN catalog_activities a ON a.id=e.activity_id
            WHERE r.observed_at>=%s AND a.publication='PUBLISHED'""", (since,)).fetchall()
        subjects = conn.execute("SELECT * FROM catalog_subjects").fetchall()
    print(json.dumps({"observed_since": since, **analyze(resources, candidates, evidence, subjects)}, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
