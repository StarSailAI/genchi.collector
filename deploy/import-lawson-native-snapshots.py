#!/usr/bin/env python3
"""Import remote-only browser snapshots through the framework sink.

Run inside the worker after copying its public detail snapshots outside the
container. Pause the product normalizer, back up first, preview, then --apply.
Original resource versions remain in allfeeds.resource_versions. Use the product
schedule_quality native/labels repair before resuming normalization.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from allfeeds_contracts import ResourceRecord
from allfeeds_worker.sink import PostgresSink
from genchi_fetchers.lawson import detail_url, merge_details, parse_detail


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot_dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sink = PostgresSink()
    with sink._connect() as conn:
        resources = conn.execute(
            "SELECT * FROM resources WHERE attributes->>'source_type'='lawson_ticket' ORDER BY id"
        ).fetchall()
    report = []
    for row in resources:
        old = row["attributes"].get("ticket_page") or {}
        code = str(old.get("pageId") or "")
        if not (args.snapshot_dir / (code + ".json")).exists():
            report.append({"id": row["id"], "code": code, "status": "missing_snapshot"})
            continue
        pages = []
        errors = []
        for f in [
            args.snapshot_dir / (code + ".json"),
            *sorted(args.snapshot_dir.glob(code + "-*.json")),
        ]:
            raw = json.loads(f.read_text())
            if not raw.get("data"):
                errors.append(raw.get("error") or "No native form-data")
                continue
            try:
                pages.append(
                    parse_detail(
                        '<script type="application/json" id="form-data">'
                        + json.dumps(raw["data"])
                        + "</script>",
                        raw["url"],
                    )
                )
            except ValueError as exc:
                errors.append(str(exc))
        attrs = {**row["attributes"], "schedule_audit": "2026-09-session-semantics"}
        payload = {**old, "searchSummary": old.get("searchSummary") or old.get("events") or []}
        expected = set(pages[0]["roundUrls"]) if pages else set()
        received = {detail_url(*p["selectedReception"].split(":")) for p in pages}
        complete = bool(pages) and expected <= received and not errors
        payload.update(
            scheduleCompleteness="native_detail" if complete else "search_summary",
            detailUrls=sorted(received),
            detailError="; ".join(errors)[:500] or None,
        )
        if complete:
            try:
                payload["events"] = merge_details(pages)
            except ValueError as exc:
                complete = False
                payload.update(scheduleCompleteness="search_summary", detailError=str(exc)[:500])
        attrs["ticket_page"] = payload
        record = ResourceRecord(
            external_id=row["external_id"],
            kind=row["kind"],
            url=row["url"],
            title=row["title"],
            content=row["content"],
            content_type=row["content_type"],
            language=row["language"],
            published_at=row["published_at"],
            attributes=attrs,
            tags=tuple(row["tags"] or []),
        )
        item = {
            "id": row["id"],
            "code": code,
            "status": "native_detail" if complete else "review",
            "sessions": len(payload.get("events") or []),
            "rounds": len(received),
            "expected_rounds": len(expected),
        }
        if args.apply:
            item["write"] = sink.write_records(row["source_id"], [record])
        report.append(item)
    print(json.dumps({"apply": args.apply, "resources": report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
