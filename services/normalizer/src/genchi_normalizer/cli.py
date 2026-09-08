from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from psycopg.types.json import Jsonb

from .app import Normalizer, Settings, run
from .legacy import import_legacy


def _alembic_config() -> Config:
    source_root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("GENCHI_ALEMBIC_ROOT", "").strip()
    if configured:
        root = Path(configured)
    elif (source_root / "alembic.ini").exists():
        root = source_root
    else:
        root = Path(sys.prefix) / "genchi_normalizer_migrations"
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return config


def _review(normalizer: Normalizer, args: argparse.Namespace) -> None:
    if args.review_command == "list":
        with normalizer.connect() as conn:
            rows = conn.execute(
                """
                SELECT c."id",c."kind",c."confidence",c."status",c."createdAt",
                    i."titleOriginal",i."canonicalUrl"
                FROM "ExtractionCandidate" c JOIN "ContentItem" i ON i."id"=c."contentItemId"
                WHERE (%s IS NULL OR c."status"=%s) ORDER BY c."createdAt" DESC LIMIT %s
                """,
                (args.status, args.status, args.limit),
            ).fetchall()
        print(json.dumps(rows, default=str, ensure_ascii=False, indent=2))
    elif args.review_command == "show":
        with normalizer.connect() as conn:
            row = conn.execute(
                'SELECT * FROM "ExtractionCandidate" WHERE "id"=%s', (args.id,)
            ).fetchone()
        if not row:
            raise SystemExit(f"candidate not found: {args.id}")
        print(json.dumps(row, default=str, ensure_ascii=False, indent=2))
    elif args.review_command == "approve":
        entity_id, errors = normalizer.apply_candidate(args.id, force=True, reviewer=args.reviewer)
        if errors:
            raise SystemExit("cannot approve: " + "; ".join(errors))
        print(json.dumps({"approved": True, "entity_id": entity_id}))
    else:
        with normalizer.connect() as conn, conn.transaction():
            result = conn.execute(
                """
                UPDATE "ExtractionCandidate" SET "status"='REJECTED',"reviewedBy"=%s,
                    "reviewedAt"=NOW(),"validationErrors"=%s,"updatedAt"=NOW()
                WHERE "id"=%s RETURNING "id"
                """,
                (args.reviewer, Jsonb([args.reason]), args.id),
            ).fetchone()
        if not result:
            raise SystemExit(f"candidate not found: {args.id}")
        print(json.dumps({"rejected": True}))


def _reprocess(normalizer: Normalizer, source_id: str) -> None:
    with normalizer.connect() as conn, conn.transaction():
        rows = conn.execute(
            """
            UPDATE "NormalizationJob" AS job SET
                "status"='PENDING',"attempts"=0,"notBefore"=NOW(),"lockedAt"=NULL,
                "lastError"=NULL,"finishedAt"=NULL,"updatedAt"=NOW()
            FROM allfeeds.resources AS resource
            WHERE job."resourceId"=resource.id
              AND job."contentHash"=resource.content_hash
              AND resource.source_id=%s
            RETURNING job."id"
            """,
            (source_id,),
        ).fetchall()
    print(json.dumps({"source": source_id, "queued": len(rows)}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(prog="genchi-normalizer")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("run")
    sub.add_parser("once")
    legacy = sub.add_parser("import-legacy")
    legacy.add_argument("--source-url", required=True)
    legacy.add_argument("--source-schema", default="public")
    legacy.add_argument("--dry-run", action="store_true")
    reprocess = sub.add_parser("reprocess")
    reprocess.add_argument("--source", required=True)
    activity_backfill = sub.add_parser("activity-backfill")
    activity_backfill.add_argument("--limit", type=int)
    review = sub.add_parser("review")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_list = review_sub.add_parser("list")
    review_list.add_argument("--status", default="PENDING")
    review_list.add_argument("--limit", type=int, default=50)
    review_show = review_sub.add_parser("show")
    review_show.add_argument("id")
    review_approve = review_sub.add_parser("approve")
    review_approve.add_argument("id")
    review_approve.add_argument("--reviewer", default="cli")
    review_reject = review_sub.add_parser("reject")
    review_reject.add_argument("id")
    review_reject.add_argument("--reason", required=True)
    review_reject.add_argument("--reviewer", default="cli")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "migrate":
        command.upgrade(_alembic_config(), "head")
        return
    settings = Settings.from_env()
    if args.command == "run":
        run(settings)
    elif args.command == "once":
        Normalizer(settings).process_once()
    elif args.command == "import-legacy":
        counts = import_legacy(
            Normalizer(settings),
            args.source_url,
            source_schema=args.source_schema,
            dry_run=args.dry_run,
        )
        print(json.dumps({"dry_run": args.dry_run, "inserted": counts}, ensure_ascii=False))
    elif args.command == "reprocess":
        _reprocess(Normalizer(settings), args.source)
    elif args.command == "activity-backfill":
        counts = Normalizer(settings).backfill_activity_profiles(limit=args.limit)
        print(json.dumps(counts, ensure_ascii=False))
    else:
        _review(Normalizer(settings), args)


if __name__ == "__main__":
    main()
