"""Evidence-preserving schedule repair. Dry-run by default; no repair emails."""

from __future__ import annotations

import argparse
import json
from collections import Counter

from psycopg.types.json import Jsonb

from .domain import Moment, classify, fingerprint, normalize
from .naming import sync_name
from .pipeline import structured
from .schedules import occurrence_label, schedule_node
from .store import Catalog, json_value

TIME_FIELDS = ("precision", "starts_at", "ends_at", "starts_on", "ends_on", "timezone")
GENERIC = ("活动开始", "活動開始", "活动进行期间", "活動進行期間")


def repair_labels(catalog: Catalog, *, apply=False):
    """Change only synthetic generic nodes with a single, active occurrence."""
    changes = []
    with catalog.connect() as conn, conn.transaction():
        rows = conn.execute(
            """SELECT m.*,a.title activity_title,a.kind activity_kind,
            (SELECT jsonb_agg(to_jsonb(o)) FROM catalog_milestone_scopes s
             JOIN catalog_occurrences o ON o.id=s.occurrence_id
             WHERE s.milestone_id=m.id AND o.status<>'SUPERSEDED') occurrences
            FROM catalog_milestones m JOIN catalog_activities a ON a.id=m.activity_id
            WHERE m.status='CONFIRMED' AND m.kind IN ('START','PERIOD') AND m.title=ANY(%s)
            ORDER BY m.activity_id,m.id FOR UPDATE OF m""",
            (list(GENERIC),),
        ).fetchall()
        for row in rows:
            scopes = row["occurrences"] or []
            if len(scopes) != 1:
                changes.append(
                    {"id": row["id"], "action": "review_ambiguous_scope", "scopes": len(scopes)}
                )
                if apply:
                    conn.execute(
                        "UPDATE catalog_milestones SET status='REVIEW',updated_at=NOW() WHERE id=%s",
                        (row["id"],),
                    )
                    catalog.review(
                        conn,
                        key="schedule-scope:" + row["id"],
                        reason="通用开始节点缺少唯一有效场次，需核对原文",
                        activity_id=row["activity_id"],
                        payload={"milestone_id": row["id"]},
                    )
                continue
            occurrence = scopes[0]
            moment = Moment.model_validate({k: occurrence[k] for k in TIME_FIELDS})
            kind = classify(row["activity_title"], row["activity_kind"])
            node_kind, title, role = schedule_node(
                row["activity_title"], kind, moment, occurrence["venue"]
            )
            details = {
                **row["details"],
                "schedule_role": role,
                "generated_from_occurrence": True,
                "quality_policy": "session-semantics-v1",
            }
            changes.append({"id": row["id"], "action": role, "title": title, "kind": node_kind})
            if not apply:
                continue
            conn.execute(
                """UPDATE catalog_milestones SET title=%s,kind=%s,details=%s,
                revision=revision+1,updated_at=NOW() WHERE id=%s""",
                (title, node_kind, Jsonb(details), row["id"]),
            )
            label = occurrence_label(moment, occurrence["venue"])
            # Preserve explicit editorial session labels; replace only the old activity-title default.
            if occurrence["label"] in (row["activity_title"], row["title"], ""):
                conn.execute(
                    "UPDATE catalog_occurrences SET label=%s WHERE id=%s", (label, occurrence["id"])
                )
                sync_name(conn, "OCCURRENCE", occurrence["id"])
            if kind != row["activity_kind"] and kind in {"EXHIBITION", "CAFE", "POPUP"}:
                conn.execute(
                    "UPDATE catalog_activities SET kind=%s,updated_at=NOW() WHERE id=%s",
                    (kind, row["activity_id"]),
                )
            sync_name(conn, "MILESTONE", row["id"], approved=title)
            catalog.change(
                conn,
                row["activity_id"],
                row["id"],
                "DATA_REPAIRED",
                "校正场次时间含义及名称",
                {k: row[k] for k in ("title", "kind", "details")},
                changes[-1],
                True,
            )
        if apply and changes:
            conn.execute(
                """UPDATE genchi_private.mail_queue SET status='CANCELED'
                WHERE milestone_id=ANY(%s) AND status IN ('PENDING','SENDING')""",
                ([r["id"] for r in changes],),
            )
    return {
        "applied": apply,
        "count": len(changes),
        "roles": dict(Counter(r["action"] for r in changes)),
        "changes": changes,
    }


def repair_native(catalog: Catalog, *, apply=False):
    """Replay audited native details into their already-mapped activities.

    Coarse dates are superseded only when the same venue/date has verified native
    time coverage. Missing/expired source dates remain explicitly reviewable.
    Never replaces organizer-reviewed performances with ticket-vendor entry times.
    """
    reports = []
    with catalog.connect() as conn, conn.transaction():
        resources = conn.execute("""SELECT * FROM allfeeds.resources
            WHERE attributes->>'schedule_audit'='2026-09-session-semantics' ORDER BY id""").fetchall()
        subjects = conn.execute("SELECT * FROM catalog_subjects").fetchall()
        for resource in resources:
            payload = resource["attributes"]["ticket_page"]
            code = payload["pageId"]
            legacy = payload.get("searchSummary") or []
            keys = ["native:lawson:event:" + str(e["id"]) for e in legacy]
            mappings = conn.execute(
                "SELECT * FROM catalog_external_ids WHERE key=ANY(%s)", (keys,)
            ).fetchall()
            targets = {r["activity_id"] for r in mappings}
            report = {
                "resource_id": resource["id"],
                "code": code,
                "status": "review",
                "legacy_dates": len(mappings),
            }
            reports.append(report)
            if len(targets) != 1:
                report["reason"] = "没有唯一已发布活动映射；保留审核，不凭标题猜测合并"
                continue
            aid = next(iter(targets))
            activity = conn.execute(
                "SELECT * FROM catalog_activities WHERE id=%s FOR UPDATE", (aid,)
            ).fetchone()
            report["activity_id"] = aid
            if activity["publication"] != "PUBLISHED":
                report["reason"] = "原记录未发布，不在历史修复中自动发布"
                continue
            if payload.get("scheduleCompleteness") != "native_detail":
                report["reason"] = "原生详情不完整，旧日期摘要不能证明具体场次"
                if apply:
                    catalog.review(
                        conn,
                        key="schedule-native:" + str(resource["id"]),
                        resource_id=resource["id"],
                        activity_id=aid,
                        reason=report["reason"],
                        payload={"resource_id": resource["id"], "code": code},
                    )
                continue
            try:
                items = structured(resource, subjects)
            except ValueError as exc:
                report["reason"] = str(exc)[:500]
                if apply:
                    catalog.review(conn, key="schedule-validation:" + str(resource["id"]),
                                   resource_id=resource["id"], activity_id=aid,
                                   reason=report["reason"], payload={"code": code})
                continue
            # A ticket page's 入店开始 is not the organizer's 开演. Preserve both meanings.
            if any(
                i.occurrence_role == "ADMISSION"
                and classify(i.title) in {"LIVE", "FESTIVAL", "MEETUP"}
                for i in items
            ):
                official = conn.execute("SELECT 1 FROM catalog_evidence WHERE activity_id=%s AND verified AND method LIKE 'editorial:official%%' LIMIT 1", (aid,)).fetchone()
                report["status"] = "organizer_schedule_preserved" if official else "entry_time_requires_review"
                report["reason"] = "售票站只明确入场，不据此改写演出时间；既有安排保留"
                if apply and not official:
                    catalog.review(conn, key="schedule-entry:" + str(resource["id"]),
                                   activity_id=aid, resource_id=resource["id"], reason=report["reason"],
                                   payload={"code": code})
                continue
            marker = fingerprint(
                "schedule-native-v1:" + str(resource["id"]) + ":" + resource["content_hash"]
            )
            if conn.execute(
                "SELECT 1 FROM catalog_changes WHERE kind='DATA_REPAIRED' AND after_value->>'repair_key'=%s",
                (marker,),
            ).fetchone():
                report["status"] = "already_repaired"
                continue
            group = "native:lawson:lcode:" + str(code) + ":" + min(i.time.anchor()[:4] for i in items)
            existing = conn.execute(
                "SELECT activity_id FROM catalog_external_ids WHERE key=%s", (group,)
            ).fetchone()
            if existing and existing["activity_id"] != aid:
                raise ValueError("Native L-code group is already mapped to another activity")
            old_ids = [m["occurrence_id"] for m in mappings if m["occurrence_id"]]
            old = conn.execute(
                "SELECT * FROM catalog_occurrences WHERE id=ANY(%s) AND status<>'SUPERSEDED'",
                (old_ids,),
            ).fetchall()
            covered = []
            unresolved = []
            for o in old:
                if o["precision"] != "DATE" or (o["ends_on"] and o["ends_on"] != o["starts_on"]):
                    continue
                matches = [
                    i
                    for i in items
                    if i.time.precision == "TIME"
                    and i.time.anchor() == str(o["starts_on"])
                    and normalize(i.venue or "") == normalize(o["venue"] or "")
                ]
                explicit_date = any(i.time.precision == "DATE"
                                    and i.time.anchor() == str(o["starts_on"])
                                    and normalize(i.venue or "") == normalize(o["venue"] or "")
                                    for i in items)
                if matches and not explicit_date:
                    covered.append(o["id"])
                elif not any(i.time.anchor() == str(o["starts_on"])
                             and normalize(i.venue or "") == normalize(o["venue"] or "")
                             for i in items):
                    unresolved.append(o["id"])
            report.update(
                status="repaired" if apply else "ready",
                native_sessions=len(items),
                superseded_dates=covered,
                unresolved_dates=unresolved,
            )
            if not apply:
                continue
            conn.execute(
                "INSERT INTO catalog_external_ids(key,activity_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (group, aid),
            )
            for item in items:
                # Retain the reviewed activity identity/name, even if the native page uses a shorter title.
                item = item.model_copy(
                    update={
                        "activity_key": group,
                        "title": activity["title"],
                        "publication": "PUBLISHED",
                    }
                )
                catalog.publish(item, historical=True, conn=conn)
            if covered:
                conn.execute(
                    "UPDATE catalog_occurrences SET status='SUPERSEDED' WHERE id=ANY(%s)",
                    (covered,),
                )
                obsolete = conn.execute(
                    """SELECT DISTINCT m.id FROM catalog_milestones m
                    JOIN catalog_milestone_scopes s ON s.milestone_id=m.id
                    WHERE s.occurrence_id=ANY(%s) AND m.status NOT IN ('SUPERSEDED','CANCELED')
                    AND NOT EXISTS(SELECT 1 FROM catalog_milestone_scopes s2 JOIN catalog_occurrences o2 ON o2.id=s2.occurrence_id
                       WHERE s2.milestone_id=m.id AND o2.status<>'SUPERSEDED')""",
                    (covered,),
                ).fetchall()
                obsolete_ids = [r["id"] for r in obsolete]
                conn.execute(
                    "UPDATE catalog_milestones SET status='SUPERSEDED',updated_at=NOW() WHERE id=ANY(%s)",
                    (obsolete_ids,),
                )
                conn.execute(
                    "UPDATE genchi_private.mail_queue SET status='CANCELED' WHERE milestone_id=ANY(%s) AND status IN ('PENDING','SENDING')",
                    (obsolete_ids,),
                )
                report["superseded_milestones"] = len(obsolete_ids)
            if unresolved:
                catalog.review(
                    conn,
                    key="schedule-unresolved:" + str(resource["id"]),
                    activity_id=aid,
                    resource_id=resource["id"],
                    reason="部分历史日期已不在当前详情中，不能推断取消或编造场次；须查历史公告",
                    payload={"occurrence_ids": unresolved},
                )
            catalog.change(
                conn,
                aid,
                None,
                "DATA_REPAIRED",
                "以原生逐场详情恢复场次及受付适用关系",
                None,
                {**report, "repair_key": marker},
                True,
            )
    return {
        "applied": apply,
        "resources": len(reports),
        "statuses": dict(Counter(r["status"] for r in reports)),
        "reports": reports,
    }


def repair_pia_status(catalog: Catalog, *, apply=False):
    """Withdraw legacy whole-page cancellation claims, never guess a replacement."""
    with catalog.connect() as conn, conn.transaction():
        rows = conn.execute("""SELECT m.* FROM catalog_milestones m
            WHERE m.platform='pia' AND m.status='CANCELED'
            AND EXISTS(SELECT 1 FROM catalog_evidence e WHERE e.milestone_id=m.id
                AND e.method='structured' AND e.excerpt LIKE '%%CANCELED%%'
                AND e.excerpt NOT LIKE '%%statusEvidence%%')
            AND NOT EXISTS(SELECT 1 FROM catalog_evidence e WHERE e.milestone_id=m.id
                AND e.verified AND (e.method LIKE 'editorial:%%' OR e.method='human'
                    OR e.excerpt LIKE '%%statusEvidence%%')) FOR UPDATE OF m""").fetchall()
        if apply:
            for row in rows:
                details = {
                    **row["details"],
                    "quality_policy": "pia-status-scope-v1",
                    "previous_unverified_status": "CANCELED",
                }
                conn.execute(
                    """UPDATE catalog_milestones SET status='REVIEW',details=%s,
                    notes=concat_ws(E'\\n',notes,'原取消状态来自整页文字匹配，缺少受付状态区证据，需重新核验'),
                    revision=revision+1,updated_at=NOW() WHERE id=%s""",
                    (Jsonb(details), row["id"]),
                )
                catalog.review(
                    conn,
                    key="pia-status:" + row["id"],
                    activity_id=row["activity_id"],
                    reason="旧规则可能把页脚取消退款说明误作受付取消；须核对售票状态区",
                    payload={"milestone_id": row["id"], "url": row["url"]},
                )
                catalog.change(
                    conn,
                    row["activity_id"],
                    row["id"],
                    "DATA_REPAIRED",
                    "撤回缺少有效证据的取消判断",
                    {"status": "CANCELED"},
                    {"status": "REVIEW", "details": details},
                    True,
                )
            conn.execute(
                "UPDATE genchi_private.mail_queue SET status='CANCELED' WHERE milestone_id=ANY(%s) AND status IN ('PENDING','SENDING')",
                ([r["id"] for r in rows],),
            )
        return {
            "applied": apply,
            "count": len(rows),
            "activity_count": len({r["activity_id"] for r in rows}),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["labels", "native", "pia-status"])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = {"labels": repair_labels, "native": repair_native, "pia-status": repair_pia_status}[args.command](Catalog(), apply=args.apply)
    print(json.dumps(json_value(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
