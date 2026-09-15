from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .domain import ActivityInput, EvidenceInput, MilestoneInput, Moment, fingerprint, normalize
from .matching import event_reference, find_activity_matches, same_milestone_fact
from .naming import sync_name
from .schedules import occurrence_label, schedule_node
from .venues import venue_key


def uid() -> str:
    return str(uuid.uuid4())


def json_value(value):
    return json.loads(json.dumps(value, default=str))


class Catalog:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ["DATABASE_URL"]

    def connect(self):
        return psycopg.connect(
            self.dsn, row_factory=dict_row, options="-c search_path=genchi,public"
        )

    @staticmethod
    def review(
        conn,
        *,
        key: str,
        reason: str,
        payload: dict,
        activity_id=None,
        resource_id=None,
        kind="EXTRACTION",
    ):
        conn.execute(
            """INSERT INTO catalog_reviews(id,activity_id,resource_id,kind,reason,payload)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET reason=EXCLUDED.reason,
            payload=EXCLUDED.payload,updated_at=NOW() WHERE catalog_reviews.status='PENDING' """,
            (fingerprint(key), activity_id, resource_id, kind, reason, Jsonb(json_value(payload))),
        )

    @staticmethod
    def evidence(conn, activity_id: str, value: EvidenceInput, milestone_id: str | None = None) -> str:
        evidence_id = fingerprint(
            "|".join(
                [
                    activity_id,
                    milestone_id or "",
                    value.source_id or "",
                    value.external_id or "",
                    value.version_hash or "",
                    value.field_path,
                    value.excerpt,
                    value.method,
                    str(value.verified),
                ]
            )
        )
        conn.execute(
            """INSERT INTO catalog_evidence(id,activity_id,milestone_id,source_id,external_id,
            version_hash,url,excerpt,field_path,method,verified,published_at,observed_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
            (
                evidence_id,
                activity_id,
                milestone_id,
                value.source_id,
                value.external_id,
                value.version_hash,
                value.url,
                value.excerpt,
                value.field_path,
                value.method,
                value.verified,
                value.published_at,
                value.observed_at or datetime.now(UTC),
            ),
        )
        return evidence_id

    @staticmethod
    def change(conn, activity_id, milestone_id, kind, summary, before, after, historical):
        conn.execute(
            """INSERT INTO catalog_changes(activity_id,milestone_id,kind,summary,before_value,after_value,notify)
            VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (
                activity_id,
                milestone_id,
                kind,
                summary,
                Jsonb(json_value(before)),
                Jsonb(json_value(after)),
                not historical,
            ),
        )

    def publish(self, item: ActivityInput, *, historical: bool = False, conn=None) -> str:
        if conn is None:
            with self.connect() as connection, connection.transaction():
                return self.publish(item, historical=historical, conn=connection)
        # A title is a candidate match, scoped to an edition. A persistent source mapping wins on updates.
        year = item.time.anchor()[:4]
        key = fingerprint(f"{normalize(item.title)}:{year}:{item.attendance}")
        origin = conn.execute(
            "SELECT attributes->>'source_type' source_type FROM allfeeds.resources WHERE source_id=%s AND external_id=%s",
            (item.evidence.source_id, item.evidence.external_id),
        ).fetchone()
        if origin and origin["source_type"] == "aggregator":
            # Broad publishers reuse generic event titles. They must pass the
            # cross-source evidence/date match, never the legacy title/year key.
            key = fingerprint("aggregator:" + item.source_key)
        reference = event_reference(item.url)
        if reference:
            # Serialize different publishers of the same event before looking up
            # titles, so simultaneous approved records cannot create duplicates.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("event-ref:" + reference,))
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
        mapped = conn.execute(
            "SELECT * FROM catalog_external_ids WHERE key=%s", (item.source_key,)
        ).fetchone()
        activity = conn.execute(
            "SELECT * FROM catalog_activities WHERE id=%s FOR UPDATE"
            if mapped
            else "SELECT * FROM catalog_activities WHERE identity_key=%s FOR UPDATE",
            (mapped["activity_id"] if mapped else key,),
        ).fetchone()
        if item.activity_key:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (item.activity_key,)
            )
            grouped = conn.execute(
                "SELECT activity_id FROM catalog_external_ids WHERE key=%s", (item.activity_key,)
            ).fetchone()
            if grouped:
                if activity and activity["id"] != grouped["activity_id"]:
                    self.merge(conn, activity["id"], grouped["activity_id"])
                activity = conn.execute(
                    "SELECT * FROM catalog_activities WHERE id=%s FOR UPDATE",
                    (grouped["activity_id"],),
                ).fetchone()
                if mapped:
                    mapped = conn.execute(
                        "SELECT * FROM catalog_external_ids WHERE key=%s", (item.source_key,)
                    ).fetchone()
        if not activity and not mapped:
            exact = [m for m in find_activity_matches(conn, item) if m["strength"] == "exact_event_and_dates"]
            if len(exact) == 1:
                activity = conn.execute("SELECT * FROM catalog_activities WHERE id=%s FOR UPDATE", (exact[0]["activity_id"],)).fetchone()
            elif len(exact) > 1:
                # The calling extraction/review transaction retains the input;
                # never guess among multiple matching activities.
                raise ValueError("Ambiguous cross-source activity identity; set an explicit reviewed activity_key")
        created = activity is None
        if created:
            activity_id = uid()
            conn.execute(
                """INSERT INTO catalog_activities(id,identity_key,title,title_zh,kind,attendance,status,
                publication,summary,official_url) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    activity_id,
                    key,
                    item.title,
                    item.title_zh,
                    item.kind,
                    item.attendance,
                    item.status,
                    item.publication,
                    item.summary,
                    item.url,
                ),
            )
            self.change(
                conn,
                activity_id,
                None,
                "NEW_ACTIVITY",
                "首次收录活动",
                None,
                {"title": item.title, "status": item.status},
                historical,
            )
        else:
            activity_id = activity["id"]
            # Re-imported legacy data cannot silently undo a human decision or a cancellation.
            conn.execute(
                """UPDATE catalog_activities SET title_zh=COALESCE(title_zh,%s),summary=COALESCE(summary,%s),
                official_url=COALESCE(official_url,%s) WHERE id=%s""",
                (item.title_zh, item.summary, item.url, activity_id),
            )
            if (
                mapped
                and item.evidence.verified
                and not historical
                and item.status != activity["status"]
            ):
                conn.execute(
                    "UPDATE catalog_activities SET status=%s,revision=revision+1,updated_at=NOW() WHERE id=%s",
                    (item.status, activity_id),
                )
                self.change(
                    conn,
                    activity_id,
                    None,
                    "STATUS_CHANGED",
                    "活动状态更新",
                    activity["status"],
                    item.status,
                    historical,
                )
                if item.status == "CANCELED":
                    conn.execute(
                        "UPDATE genchi_private.mail_queue SET status='CANCELED' WHERE activity_id=%s AND status IN ('PENDING','SENDING')",
                        (activity_id,),
                    )
        if item.activity_key:
            conn.execute(
                "INSERT INTO catalog_external_ids(key,activity_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (item.activity_key, activity_id),
            )
            if item.evidence.verified and item.title != (activity or {}).get("title", item.title):
                conn.execute(
                    "UPDATE catalog_activities SET title=%s,revision=revision+1,updated_at=NOW() WHERE id=%s",
                    (item.title, activity_id),
                )
        activity_evidence_id = self.evidence(conn, activity_id, item.evidence)
        relations = {relation.subject_slug: relation for relation in item.subject_relations}
        for slug in item.subject_slugs:
            relation = relations.get(slug)
            relation_evidence_id = (
                self.evidence(conn, activity_id, relation.evidence) if relation else activity_evidence_id
            )
            conn.execute(
                """INSERT INTO catalog_activity_subjects(
                    activity_id,subject_slug,relation_kind,participant_name,scope_note,evidence_id,verified)
                SELECT %s,slug,%s,%s,%s,%s,%s FROM catalog_subjects WHERE slug=%s
                ON CONFLICT(activity_id,subject_slug) DO UPDATE SET
                  relation_kind=EXCLUDED.relation_kind,
                  participant_name=EXCLUDED.participant_name,
                  scope_note=EXCLUDED.scope_note,
                  evidence_id=EXCLUDED.evidence_id,
                  verified=EXCLUDED.verified
                WHERE EXCLUDED.verified OR NOT catalog_activity_subjects.verified""",
                (
                    activity_id,
                    relation.relation_kind if relation else "SOURCE_SCOPE",
                    relation.participant_name if relation else None,
                    relation.scope_note if relation else None,
                    relation_evidence_id,
                    relation.evidence.verified if relation else item.evidence.verified,
                    slug,
                ),
            )
        occurrence_id = None
        if item.occurrence_key:
            clock = (
                item.time.starts_at.astimezone(UTC).isoformat()
                if item.time.starts_at
                else item.time.anchor()
            )
            period = bool(item.time.ends_on and item.time.ends_on != item.time.starts_on)
            occurrence_key = fingerprint(f"{clock}:{venue_key(item.venue, item.title, year)}" + (":period" if period else ""))
            occurrence = conn.execute(
                "SELECT * FROM catalog_occurrences WHERE id=%s"
                if mapped and mapped["occurrence_id"]
                else "SELECT * FROM catalog_occurrences WHERE activity_id=%s AND identity_key=%s",
                (mapped["occurrence_id"],)
                if mapped and mapped["occurrence_id"]
                else (activity_id, occurrence_key),
            ).fetchone()
            occurrence_id = occurrence["id"] if occurrence else uid()
            values = (
                item.venue,
                item.city,
                item.time.starts_at,
                item.time.ends_at,
                item.time.starts_on,
                item.time.ends_on,
                item.time.precision,
                item.time.timezone,
            )
            if occurrence is None:
                conn.execute(
                    """INSERT INTO catalog_occurrences(id,activity_id,identity_key,label,venue,city,starts_at,
                    ends_at,starts_on,ends_on,precision,timezone) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (occurrence_id, activity_id, occurrence_key, occurrence_label(item.time, item.venue, item.occurrence_label), *values),
                )
            elif not historical and item.evidence.verified:
                conn.execute(
                    """UPDATE catalog_occurrences SET venue=%s,city=%s,starts_at=%s,ends_at=%s,starts_on=%s,
                    ends_on=%s,precision=%s,timezone=%s,label=COALESCE(%s,label) WHERE id=%s""",
                    (*values, item.occurrence_label, occurrence_id),
                )
        conn.execute(
            """INSERT INTO catalog_external_ids(key,activity_id,occurrence_id) VALUES(%s,%s,%s)
            ON CONFLICT(key) DO UPDATE SET occurrence_id=COALESCE(EXCLUDED.occurrence_id,catalog_external_ids.occurrence_id)""",
            (item.source_key, activity_id, occurrence_id),
        )
        if occurrence_id:
            kind, start_title, role = schedule_node(item.title, item.kind, item.time, item.venue,
                                                   item.occurrence_label, item.occurrence_role)
            start = MilestoneInput(
                source_key=f"occurrence:{occurrence_id}",
                kind=kind,
                title=start_title,
                title_zh=start_title,
                time=item.time,
                url=item.url,
                evidence=item.evidence,
                notes="当前来源仅确认日期，具体时刻尚未核验" if item.time.precision == "DATE" else None,
                details={"schedule_role": role, "generated_from_occurrence": True},
            )
            self.milestone(conn, activity_id, occurrence_id, start, historical=historical)
        for milestone in item.milestones:
            self.milestone(conn, activity_id, occurrence_id, milestone, historical=historical)
        sync_name(
            conn,
            "ACTIVITY",
            activity_id,
            approved=item.title_zh if item.evidence.method == "human" else None,
        )
        if occurrence_id:
            sync_name(conn, "OCCURRENCE", occurrence_id)
        return activity_id

    def milestone(
        self, conn, activity_id, occurrence_id, item: MilestoneInput, *, historical=False
    ):
        source_key = f"{activity_id}:{item.source_key}"
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (activity_id,))
        conn.execute("SELECT id FROM catalog_activities WHERE id=%s FOR UPDATE", (activity_id,))
        mapped = conn.execute(
            "SELECT milestone_id FROM catalog_external_ids WHERE key=%s", (source_key,)
        ).fetchone()
        semantic = fingerprint(
            "|".join(
                [
                    item.kind,
                    normalize(item.title),
                    item.platform or "",
                    item.url or "",
                    item.scope_key or item.round_key
                    or (
                        str(occurrence_id)
                        if item.kind in {"START", "PERIOD", "DOORS"}
                        else item.time.anchor()
                    ),
                ]
            )
        )
        current = conn.execute(
            "SELECT * FROM catalog_milestones WHERE id=%s"
            if mapped and mapped["milestone_id"]
            else "SELECT * FROM catalog_milestones WHERE activity_id=%s AND identity_key=%s",
            (mapped["milestone_id"],)
            if mapped and mapped["milestone_id"]
            else (activity_id, semantic),
        ).fetchone()
        if current is None and not mapped:
            alternatives = conn.execute(
                """SELECT m.*,EXISTS(SELECT 1 FROM catalog_milestone_scopes s
                WHERE s.milestone_id=m.id AND s.occurrence_id=%s) same_occurrence
                FROM catalog_milestones m WHERE m.activity_id=%s AND m.kind=%s""",
                (occurrence_id, activity_id, item.kind),
            ).fetchall()
            identical = [r for r in alternatives if same_milestone_fact(item, r, same_occurrence=r["same_occurrence"])]
            if len(identical) == 1:
                milestone_id = identical[0]["id"]
                conn.execute("INSERT INTO catalog_external_ids(key,activity_id,milestone_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                             (source_key, activity_id, milestone_id))
                if occurrence_id:
                    conn.execute("INSERT INTO catalog_milestone_scopes VALUES(%s,%s) ON CONFLICT DO NOTHING", (milestone_id, occurrence_id))
                self.evidence(conn, activity_id, item.evidence, milestone_id)
                return
        fields = dict(
            title=item.title,
            kind=item.kind,
            **item.time.model_dump(),
            status=item.status,
            url=item.url,
            platform=item.platform,
            round_key=item.round_key,
            eligibility=item.eligibility,
            notes=item.notes,
            requires=item.requires,
            details=item.details,
        )
        if current and mapped and item.scope_key and occurrence_id and item.evidence.verified:
            differs = any(current[k] != fields[k] for k in fields)
            shared = conn.execute("""SELECT 1 FROM catalog_milestone_scopes
                WHERE milestone_id=%s AND occurrence_id<>%s LIMIT 1""",
                (current["id"], occurrence_id)).fetchone()
            if differs and shared and not historical:
                # Identical windows may share a node. A later per-session change
                # must split that scope instead of changing every other session.
                conn.execute("DELETE FROM catalog_milestone_scopes WHERE milestone_id=%s AND occurrence_id=%s",
                             (current["id"], occurrence_id))
                conn.execute("DELETE FROM catalog_external_ids WHERE key=%s", (source_key,))
                semantic = fingerprint(semantic + ":split:" + source_key + ":" + current["id"])
                current = None
        milestone_id = current["id"] if current else uid()
        changed = current and any(current[k] != fields[k] for k in fields)
        if current is None:
            conn.execute(
                """INSERT INTO catalog_milestones(id,activity_id,identity_key,title,kind,starts_at,ends_at,
                starts_on,ends_on,precision,timezone,status,url,platform,round_key,eligibility,notes,requires,details)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    milestone_id,
                    activity_id,
                    semantic,
                    item.title,
                    item.kind,
                    item.time.starts_at,
                    item.time.ends_at,
                    item.time.starts_on,
                    item.time.ends_on,
                    item.time.precision,
                    item.time.timezone,
                    item.status,
                    item.url,
                    item.platform,
                    item.round_key,
                    item.eligibility,
                    item.notes,
                    item.requires,
                    Jsonb(item.details),
                ),
            )
            self.change(
                conn,
                activity_id,
                milestone_id,
                "NEW_MILESTONE",
                f"新增：{item.title}",
                None,
                fields,
                historical,
            )
        elif changed and not historical:
            if not mapped or not item.evidence.verified:
                self.review(
                    conn,
                    key=f"conflict:{milestone_id}:{item.evidence.version_hash}",
                    reason="不同来源或未验证的节点变化，需要核对后发布",
                    activity_id=activity_id,
                    kind="CONFLICT",
                    payload={"milestone_id": milestone_id, "before": current, "after": fields},
                )
            else:
                conn.execute(
                    """UPDATE catalog_milestones SET title=%s,kind=%s,starts_at=%s,ends_at=%s,starts_on=%s,ends_on=%s,
                    precision=%s,status=%s,url=%s,notes=%s,eligibility=%s,details=%s,timezone=%s,platform=%s,round_key=%s,requires=%s,revision=revision+1,updated_at=NOW() WHERE id=%s""",
                    (
                        item.title,
                        item.kind,
                        item.time.starts_at,
                        item.time.ends_at,
                        item.time.starts_on,
                        item.time.ends_on,
                        item.time.precision,
                        item.status,
                        item.url,
                        item.notes,
                        item.eligibility,
                        Jsonb(item.details),
                        item.time.timezone,
                        item.platform,
                        item.round_key,
                        item.requires,
                        milestone_id,
                    ),
                )
                conn.execute(
                    "UPDATE genchi_private.mail_queue SET status='CANCELED' WHERE milestone_id=%s AND status IN ('PENDING','SENDING')",
                    (milestone_id,),
                )
                conn.execute(
                    "UPDATE catalog_activities SET revision=revision+1,updated_at=NOW() WHERE id=%s",
                    (activity_id,),
                )
                self.change(
                    conn,
                    activity_id,
                    milestone_id,
                    "MILESTONE_CHANGED",
                    f"更新：{item.title}",
                    {k: current[k] for k in fields},
                    fields,
                    historical,
                )
        if occurrence_id:
            conn.execute(
                "INSERT INTO catalog_milestone_scopes VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (milestone_id, occurrence_id),
            )
        conn.execute(
            """INSERT INTO catalog_external_ids(key,activity_id,milestone_id) VALUES(%s,%s,%s)
            ON CONFLICT(key) DO NOTHING""",
            (source_key, activity_id, milestone_id),
        )
        self.evidence(conn, activity_id, item.evidence, milestone_id)
        sync_name(
            conn,
            "MILESTONE",
            milestone_id,
            approved=item.title_zh if item.evidence.method == "human" else None,
        )
        return milestone_id

    def merge(self, conn, old_id: str, target_id: str):
        """Merge only when an upstream grouping identifier explicitly proves common ownership.

        Keep the old activity as an alias and preserve evidence / change history.
        Never infer a tour from approximate title or nearby calendar dates.
        """
        if old_id == target_id:
            return
        for aid in sorted([old_id, target_id]):
            conn.execute("SELECT id FROM catalog_activities WHERE id=%s FOR UPDATE", (aid,))
        occurrences = conn.execute(
            "SELECT * FROM catalog_occurrences WHERE activity_id=%s", (old_id,)
        ).fetchall()
        for occurrence in occurrences:
            duplicate = conn.execute(
                "SELECT id FROM catalog_occurrences WHERE activity_id=%s AND identity_key=%s",
                (target_id, occurrence["identity_key"]),
            ).fetchone()
            if duplicate:
                conn.execute(
                    "INSERT INTO catalog_milestone_scopes SELECT milestone_id,%s FROM catalog_milestone_scopes WHERE occurrence_id=%s ON CONFLICT DO NOTHING",
                    (duplicate["id"], occurrence["id"]),
                )
                conn.execute(
                    "DELETE FROM catalog_milestone_scopes WHERE occurrence_id=%s",
                    (occurrence["id"],),
                )
                conn.execute(
                    "UPDATE catalog_external_ids SET occurrence_id=%s WHERE occurrence_id=%s",
                    (duplicate["id"], occurrence["id"]),
                )
            else:
                conn.execute(
                    "UPDATE catalog_occurrences SET activity_id=%s WHERE id=%s",
                    (target_id, occurrence["id"]),
                )
        nodes = conn.execute(
            "SELECT * FROM catalog_milestones WHERE activity_id=%s", (old_id,)
        ).fetchall()
        for node in nodes:
            duplicate = conn.execute(
                "SELECT id FROM catalog_milestones WHERE activity_id=%s AND identity_key=%s",
                (target_id, node["identity_key"]),
            ).fetchone()
            if duplicate:
                conn.execute(
                    "INSERT INTO catalog_milestone_scopes SELECT %s,occurrence_id FROM catalog_milestone_scopes WHERE milestone_id=%s ON CONFLICT DO NOTHING",
                    (duplicate["id"], node["id"]),
                )
                conn.execute(
                    "DELETE FROM catalog_milestone_scopes WHERE milestone_id=%s", (node["id"],)
                )
                conn.execute(
                    "UPDATE catalog_external_ids SET milestone_id=%s WHERE milestone_id=%s",
                    (duplicate["id"], node["id"]),
                )
                conn.execute(
                    "UPDATE catalog_evidence SET milestone_id=%s WHERE milestone_id=%s",
                    (duplicate["id"], node["id"]),
                )
                conn.execute(
                    "UPDATE catalog_milestones SET status='SUPERSEDED' WHERE id=%s", (node["id"],)
                )
            else:
                conn.execute(
                    "UPDATE catalog_milestones SET activity_id=%s WHERE id=%s",
                    (target_id, node["id"]),
                )
        conn.execute(
            "UPDATE genchi_private.mail_queue SET status='CANCELED' WHERE activity_id=%s AND status IN ('PENDING','SENDING')",
            (old_id,),
        )
        conn.execute(
            """INSERT INTO catalog_activity_subjects(
                activity_id,subject_slug,relation_kind,participant_name,scope_note,evidence_id,verified)
            SELECT %s,subject_slug,relation_kind,participant_name,scope_note,evidence_id,verified
            FROM catalog_activity_subjects WHERE activity_id=%s
            ON CONFLICT(activity_id,subject_slug) DO UPDATE SET
              relation_kind=EXCLUDED.relation_kind,
              participant_name=EXCLUDED.participant_name,
              scope_note=EXCLUDED.scope_note,
              evidence_id=EXCLUDED.evidence_id,
              verified=EXCLUDED.verified
            WHERE EXCLUDED.verified AND NOT catalog_activity_subjects.verified""",
            (target_id, old_id),
        )
        conn.execute(
            "UPDATE catalog_evidence SET activity_id=%s WHERE activity_id=%s", (target_id, old_id)
        )
        conn.execute(
            "UPDATE catalog_changes SET activity_id=%s WHERE activity_id=%s", (target_id, old_id)
        )
        conn.execute(
            "UPDATE catalog_reviews SET activity_id=%s WHERE activity_id=%s", (target_id, old_id)
        )
        conn.execute(
            "UPDATE catalog_external_ids SET activity_id=%s WHERE activity_id=%s",
            (target_id, old_id),
        )
        conn.execute(
            "INSERT INTO catalog_external_ids(key,activity_id,occurrence_id,milestone_id) SELECT %s || substring(key FROM %s),activity_id,occurrence_id,milestone_id FROM catalog_external_ids WHERE key LIKE %s AND milestone_id IS NOT NULL ON CONFLICT DO NOTHING",
            (target_id, len(old_id) + 1, old_id + ":%"),
        )
        conn.execute(
            "INSERT INTO genchi_private.follows(id,account_id,target_type,target_id,reminder_hours,include_children,kinds,cities) SELECT id || ':merged',account_id,target_type,%s,reminder_hours,include_children,kinds,cities FROM genchi_private.follows WHERE target_type='ACTIVITY' AND target_id=%s ON CONFLICT(account_id,target_type,target_id) DO NOTHING",
            (target_id, old_id),
        )
        conn.execute(
            "DELETE FROM genchi_private.follows WHERE target_type='ACTIVITY' AND target_id=%s",
            (old_id,),
        )
        conn.execute(
            "INSERT INTO genchi_private.participation SELECT account_id,%s,round_key,status FROM genchi_private.participation WHERE activity_id=%s ON CONFLICT DO NOTHING",
            (target_id, old_id),
        )
        conn.execute("UPDATE catalog_activities SET publication='REJECTED' WHERE id=%s", (old_id,))
        conn.execute(
            "INSERT INTO catalog_external_ids(key,activity_id) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET activity_id=EXCLUDED.activity_id",
            ("redirect:" + old_id, target_id),
        )
        self.change(
            conn,
            target_id,
            None,
            "MERGED",
            "依据官方共同活动标识归并场次",
            {"previous_id": old_id},
            {"activity_id": target_id},
            True,
        )

    def approve_review(
        self, review_id: str, reviewer: str, approve: bool, edited: ActivityInput | None = None
    ):
        with self.connect() as conn, conn.transaction():
            review = conn.execute(
                "SELECT * FROM catalog_reviews WHERE id=%s FOR UPDATE", (review_id,)
            ).fetchone()
            if not review or review["status"] != "PENDING":
                return False
            payload = review["payload"]
            if approve and review["resource_id"] and payload.get("activity"):
                expected_hash = payload["activity"].get("evidence", {}).get("version_hash")
                current = conn.execute(
                    "SELECT content_hash FROM allfeeds.resources WHERE id=%s",
                    (review["resource_id"],),
                ).fetchone()
                if current and expected_hash and current["content_hash"] != expected_hash:
                    raise ValueError("原文已经更新，请核对最新候选后再发布")
            if edited is not None:
                payload = {
                    **payload,
                    "activity": edited.model_dump(mode="json"),
                    "original_payload": payload,
                }
                conn.execute(
                    "UPDATE catalog_reviews SET payload=%s WHERE id=%s", (Jsonb(payload), review_id)
                )
            if approve and "activity" in payload:
                value = ActivityInput.model_validate(payload["activity"])
                value.publication = "PUBLISHED"
                value.evidence.verified = True
                value.evidence.method = "human"
                for node in value.milestones:
                    node.evidence.verified = True
                    node.evidence.method = "human"
                for relation in value.subject_relations:
                    relation.evidence.verified = True
                    relation.evidence.method = "human"
                activity_id = self.publish(value, conn=conn)
                conn.execute(
                    "UPDATE catalog_reviews SET activity_id=%s WHERE id=%s",
                    (activity_id, review_id),
                )
                conn.execute(
                    "UPDATE catalog_activities SET publication='PUBLISHED' WHERE id=%s",
                    (activity_id,),
                )
                self.change(
                    conn,
                    activity_id,
                    None,
                    "PUBLISHED",
                    "审核后发布",
                    None,
                    {"title": value.title},
                    False,
                )
            elif approve and review["kind"] == "CONFLICT":
                node = payload["after"]
                evidence = (
                    EvidenceInput.model_validate(payload["evidence"])
                    if payload.get("evidence")
                    else EvidenceInput(
                        excerpt=f"审核确认：{review['reason']}",
                        external_id=review_id,
                        field_path="milestone",
                    )
                )
                evidence.method = "human"
                evidence.verified = True
                temporal = {
                    k: node.get(k)
                    for k in (
                        "starts_at",
                        "ends_at",
                        "starts_on",
                        "ends_on",
                        "precision",
                        "timezone",
                    )
                }
                item = MilestoneInput(
                    source_key=f"review:{review_id}",
                    evidence=evidence,
                    time=Moment.model_validate(temporal),
                    **{k: v for k, v in node.items() if k not in temporal},
                )
                conn.execute(
                    "INSERT INTO catalog_external_ids(key,activity_id,milestone_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        f"{review['activity_id']}:{item.source_key}",
                        review["activity_id"],
                        payload["milestone_id"],
                    ),
                )
                self.milestone(conn, review["activity_id"], None, item)
            elif review["activity_id"] and review["kind"] != "CONFLICT":
                conn.execute(
                    "UPDATE catalog_activities SET publication=%s,updated_at=NOW() WHERE id=%s AND publication='REVIEW'",
                    ("PUBLISHED" if approve else "REJECTED", review["activity_id"]),
                )
            conn.execute(
                "UPDATE catalog_reviews SET status=%s,reviewed_by=%s,updated_at=NOW() WHERE id=%s",
                ("APPROVED" if approve else "REJECTED", reviewer, review_id),
            )
            return True
