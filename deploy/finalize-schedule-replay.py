#!/usr/bin/env python3
"""Acknowledge reviewed snapshot versions without replaying repair notifications."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from genchi_product.domain import fingerprint
from genchi_product.pipeline import index_raw
from genchi_product.store import Catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = json.loads(args.report.read_text())
    if not result.get("applied"):
        raise ValueError("An applied native repair report is required")
    catalog = Catalog()
    counts = Counter()
    with catalog.connect() as conn, conn.transaction():
        for report in result["reports"]:
            row = conn.execute(
                "SELECT * FROM allfeeds.resources WHERE id=%s FOR UPDATE", (report["resource_id"],)
            ).fetchone()
            if not row or row["attributes"].get("schedule_audit") != "2026-09-session-semantics":
                raise ValueError("Audited resource no longer exists")
            job = conn.execute(
                "SELECT * FROM catalog_jobs WHERE resource_id=%s FOR UPDATE", (row["id"],)
            ).fetchone()
            if job and (job["content_hash"] != row["content_hash"] or job["status"] == "RUNNING"):
                raise ValueError("Normalizer must be paused at the audited version")
            done = report["status"] in {
                "repaired",
                "already_repaired",
                "organizer_schedule_preserved",
            }
            if report["status"] in {"repaired", "already_repaired"}:
                marker = fingerprint(
                    "schedule-native-v1:" + str(row["id"]) + ":" + row["content_hash"]
                )
                if not conn.execute(
                    "SELECT 1 FROM catalog_changes WHERE after_value->>'repair_key'=%s", (marker,)
                ).fetchone():
                    raise ValueError("No committed repair receipt for the current raw version")
            if (
                report["status"] == "organizer_schedule_preserved"
                and not conn.execute(
                    "SELECT 1 FROM catalog_evidence WHERE activity_id=%s AND verified AND method LIKE 'editorial:official%%'",
                    (report["activity_id"],),
                ).fetchone()
            ):
                raise ValueError("Organizer evidence missing")
            state = "DONE" if done else "REVIEW"
            counts[state] += 1
            if not args.apply:
                continue
            index_raw(conn, row)
            if not done:
                catalog.review(
                    conn,
                    key="schedule-disposition:" + str(row["id"]) + ":" + row["content_hash"],
                    activity_id=report.get("activity_id"),
                    resource_id=row["id"],
                    reason=report.get("reason") or "历史场次修复仍需审核",
                    payload=report,
                )
            conn.execute(
                """UPDATE catalog_jobs SET status=%s,lease_token=NULL,locked_at=NULL,
                last_error=%s,updated_at=NOW() WHERE resource_id=%s AND content_hash=%s""",
                (state, None if done else report.get("reason"), row["id"], row["content_hash"]),
            )
    print(json.dumps({"applied": args.apply, "jobs": dict(counts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
