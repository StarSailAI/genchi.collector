"""Read-only verification of fresh raw versions and their curated catalog mappings.

Execute inside the product image; pass an explicit timezone-aware --since.
"""
import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime

from genchi_product.pipeline import structured
from genchi_product.store import Catalog

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--since", required=True)
args = parser.parse_args()
since = datetime.fromisoformat(args.since)
if since.tzinfo is None:
    parser.error("--since requires an explicit timezone")

stats = defaultdict(Counter)
issues = []
with Catalog().connect() as conn:
    subjects = conn.execute("SELECT * FROM catalog_subjects").fetchall()
    resources = conn.execute("""SELECT r.*,j.status job_status,j.content_hash job_hash,v.id version_id FROM allfeeds.resources r
        LEFT JOIN catalog_jobs j ON j.resource_id=r.id
        LEFT JOIN allfeeds.resource_versions v ON v.resource_id=r.id AND v.content_hash=r.content_hash
        WHERE r.observed_at>%s ORDER BY r.source_id,r.id""", (since,)).fetchall()
    for resource in resources:
        source = resource['source_id']
        stats[source]['records'] += 1
        stats[source]['job_' + str(resource['job_status'] or 'MISSING')] += 1
        if resource['version_id'] is None:
            issues.append({'resource':resource['id'],'issue':'missing current immutable raw version'})
        if resource['job_hash'] != resource['content_hash']:
            issues.append({'resource':resource['id'],'issue':'catalog job does not match current raw version'})
        candidates = conn.execute("""SELECT count(*) n FROM catalog_reviews WHERE resource_id=%s
            AND payload ? 'activity' AND payload->'activity'->'evidence'->>'version_hash'=%s""",
            (resource['id'],resource['content_hash'])).fetchone()['n']
        stats[source]['current_version_candidates'] += candidates
        if resource['attributes'].get('source_type') == 'official_site':
            stats[source]['dated'] += int(resource.get('published_at') is not None)
            continue
        try:
            items = structured(resource, subjects)
        except Exception as exc:
            issues.append({'resource':resource['id'],'issue':'structured validation: '+str(exc)[:200]})
            continue
        stats[source]['parsed_occurrences'] += sum(bool(item.occurrence_key) for item in items)
        stats[source]['parsed_milestones'] += sum(len(item.milestones) for item in items)
        if resource['job_status'] != 'DONE':
            continue
        for item in items:
            if item.publication == 'REVIEW':
                stats[source]['review_candidates'] += 1
                continue
            mapped = conn.execute("SELECT * FROM catalog_external_ids WHERE key=%s",(item.source_key,)).fetchone()
            if not mapped:
                issues.append({'resource':resource['id'],'issue':'missing published external ID','key':item.source_key})
                continue
            stats[source]['mapped_occurrences'] += int(bool(mapped['occurrence_id']))
            if item.occurrence_key:
                row=conn.execute("SELECT * FROM catalog_occurrences WHERE id=%s",(mapped['occurrence_id'],)).fetchone()
                if not row or any(row[key] != value for key,value in item.time.model_dump().items()):
                    issues.append({'resource':resource['id'],'issue':'occurrence time mismatch','key':item.source_key})
            evidence = conn.execute("""SELECT count(*) n FROM catalog_evidence WHERE activity_id=%s AND source_id=%s
                AND external_id=%s AND version_hash=%s AND verified AND method='structured'""",
                (mapped['activity_id'],source,resource['external_id'],resource['content_hash'])).fetchone()['n']
            if not evidence:
                issues.append({'resource':resource['id'],'issue':'missing current structured evidence','key':item.source_key})
            for milestone in item.milestones:
                key=f"{mapped['activity_id']}:{milestone.source_key}"
                row=conn.execute("""SELECT m.* FROM catalog_external_ids e JOIN catalog_milestones m ON m.id=e.milestone_id
                    WHERE e.key=%s""",(key,)).fetchone()
                if not row:
                    issues.append({'resource':resource['id'],'issue':'missing milestone','key':key})
                else:
                    stats[source]['mapped_milestones'] += 1
                    if any(row[k] != v for k,v in milestone.time.model_dump().items()):
                        issues.append({'resource':resource['id'],'issue':'milestone time mismatch','key':key})
print(json.dumps({'since':since,'sources':stats,'issue_count':len(issues),'issues':issues},ensure_ascii=False,default=str),flush=True)
raise SystemExit(1 if issues else 0)
