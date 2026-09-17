from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .collection_digest import deliver_digest, prepare_due_digest
from .importer import import_legacy
from .inbound import InboundError, Resend, backfill, forward_one, reconcile
from .naming import normalize_catalog
from .notifications import deliver_one, plan
from .pipeline import index_raw, process_one
from .store import Catalog

REVIEW_BATCH_LIMIT = 64
REVIEW_IDLE_SECONDS = 300
REVIEW_CATCHUP_SECONDS = 30


def review_poll_seconds(selected: int) -> int:
    """Drain a full review batch promptly; keep the quiet cadence when caught up."""
    return REVIEW_CATCHUP_SECONDS if selected >= REVIEW_BATCH_LIMIT else REVIEW_IDLE_SECONDS


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    ask = sub.add_parser("ask-eval", help="Internal read-only RAG evaluation; does not use the public quota")
    ask.add_argument("question")
    ask.add_argument("--locale", choices=["zh-Hans", "zh-Hant", "en", "ja"], default="zh-Hans")
    ask.add_argument("--trace", action="store_true", help="Print tool counts/timing, never reasoning or credentials")
    search = sub.add_parser("search-eval", help="Internal evidence retrieval only; no LLM or public quota")
    search.add_argument("query", help="JSON EvidenceQuery: terms, focus, intent, time_scope")
    sub.add_parser("import-legacy")
    sub.add_parser("plan")
    sub.add_parser("index-raw")
    sub.add_parser("refresh-structured")
    sub.add_parser("inbound-backfill", help="Queue all retained Resend received mail, deduplicated")
    sub.add_parser(
        "inbound-status", help="Show forwarding counts and failures without mail contents"
    )
    sub.add_parser("collection-digest-status", help="Show daily collection digest delivery status")
    reviews = sub.add_parser("review-batch", help="Batch-audit catalog candidates with DeepSeek")
    reviews.add_argument("--limit", type=int, default=500, help="Maximum current candidates (1–5000)")
    reviews.add_argument("--batch-size", type=int, default=16, help="Candidates per model call (1–30)")
    reviews.add_argument("--source-type", help="Review one source type first, e.g. official_site")
    reviews.add_argument("--source-id", help="Review one configured source first, e.g. pia-jpop-tickets")
    review_mode = reviews.add_mutually_exclusive_group()
    review_mode.add_argument("--dry-run", action="store_true", help="Call model without database writes")
    review_mode.add_argument("--apply", action="store_true", help="Save results and publish eligible approvals")
    names = sub.add_parser("normalize-names", help="Preview/apply audited Chinese display names")
    names.add_argument("--apply", action="store_true")
    names.add_argument("--output", help="Write the complete reviewable report as JSON")
    worker = sub.add_parser("worker")
    worker.add_argument(
        "--mode",
        choices=["catalog", "reviews", "notifications", "collection-digest", "idle"],
        default="catalog",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s"
    )
    catalog = Catalog()
    if args.command == "ask-eval":
        from .ask_agent import run_agent
        from .assistant import Question, model_config

        question = Question(question=args.question).question

        def trace(event):
            print(json.dumps({"trace": event}, ensure_ascii=False, default=str))

        result = run_agent(catalog, question, model_config(), locale=args.locale,
                           trace=trace if args.trace else None)
        print(json.dumps(result, ensure_ascii=False, default=str))
    elif args.command == "search-eval":
        from .ask_agent import EvidenceStore
        from .retrieval import EvidenceQuery, search_evidence

        query = EvidenceQuery.model_validate_json(args.query)
        started = time.monotonic()
        evidence = EvidenceStore(catalog, started + 15)
        result = search_evidence(evidence, query, evidence.now)
        print(json.dumps(dict(result=result, seconds=round(time.monotonic() - started, 3)),
                         ensure_ascii=False, default=str))
    elif args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(catalog), host="0.0.0.0", port=8080, access_log=False, proxy_headers=False)
    elif args.command == "import-legacy":
        print(json.dumps(import_legacy(catalog)))
    elif args.command == "index-raw":
        with catalog.connect() as conn:
            rows = conn.execute("SELECT * FROM allfeeds.resources").fetchall()
            for resource in rows:
                index_raw(conn, resource)
            print(json.dumps({"indexed": len(rows)}))
    elif args.command == "normalize-names":
        from pathlib import Path

        result = normalize_catalog(catalog, apply=args.apply)
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in result.items() if k != "items"}, ensure_ascii=False))
    elif args.command == "refresh-structured":
        with catalog.connect() as conn:
            result = conn.execute("""UPDATE catalog_jobs j SET status='PENDING',attempts=0,not_before=NOW()
                FROM allfeeds.resources r WHERE r.id=j.resource_id AND r.kind IN
                ('ticket_act','ticket_reception','eplus_ticket_page','pia_ticket_page','lawson_ticket_page') AND (jsonb_array_length(COALESCE(r.attributes->'ticket_page'->'events',r.attributes->'eplus_ticket'->'events','[]'))>0 OR r.attributes->>'source_type'='asobi_ticket') RETURNING j.resource_id""")
            print(json.dumps({"queued": result.rowcount}))
    elif args.command == "plan":
        plan(catalog)
    elif args.command == "inbound-backfill":
        print(json.dumps(backfill(catalog)))
    elif args.command == "inbound-status":
        with catalog.connect() as conn:
            counts = conn.execute(
                "SELECT status,count(*) FROM genchi_private.inbound_mail GROUP BY status"
            ).fetchall()
            failures = conn.execute(
                "SELECT email_id,status,attempts,last_error FROM genchi_private.inbound_mail WHERE status IN ('FAILED','UNCERTAIN') ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
            print(json.dumps({"counts": counts, "failures": failures}, default=str))
    elif args.command == "collection-digest-status":
        with catalog.connect() as conn:
            state = conn.execute(
                "SELECT last_version_id,updated_at FROM genchi_private.collection_digest_state WHERE id=TRUE"
            ).fetchone()
            recent = conn.execute(
                """SELECT digest_date,status,counts,attempts,sent_at,last_error
                FROM genchi_private.collection_digests ORDER BY digest_date DESC LIMIT 14"""
            ).fetchall()
            print(json.dumps({"cursor": state, "recent": recent}, default=str))
    elif args.command == "review-batch":
        from .batch_review import run

        print(json.dumps(run(catalog, limit=args.limit, batch_size=args.batch_size,
                             apply=args.apply, dry_run=args.dry_run,
                             source_type=args.source_type, source_id=args.source_id), ensure_ascii=False))
    else:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        state = {"heartbeat": time.monotonic()}

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                max_age = 600 if args.mode == "reviews" else 180
                ok = time.monotonic() - state["heartbeat"] < max_age
                self.send_response(200 if ok else 503)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": ok, "mode": args.mode}).encode())

            def log_message(self, *_):
                pass

        health = ThreadingHTTPServer(("0.0.0.0", 8070), Health)
        threading.Thread(target=health.serve_forever, daemon=True).start()
        last_plan = 0
        last_inbound_scan = 0
        last_review = 0
        review_wait = REVIEW_IDLE_SECONDS
        inbound_client = Resend() if os.getenv("RESEND_API_KEY") else None
        while not stop.is_set():
            try:
                state["heartbeat"] = time.monotonic()
                if args.mode == "catalog":
                    busy = process_one(catalog)
                elif args.mode == "reviews":
                    if time.monotonic() - last_review >= review_wait:
                        last_review = time.monotonic()
                        review_wait = REVIEW_IDLE_SECONDS
                        from .batch_review import run

                        report = run(catalog, limit=REVIEW_BATCH_LIMIT, batch_size=16, apply=True)
                        logging.info("catalog batch review: %s",
                                     {key: value for key, value in report.items()
                                      if key != "examples"})
                        review_wait = review_poll_seconds(report["selected"])
                        last_review = time.monotonic()
                    busy = False
                elif args.mode == "notifications":
                    if time.monotonic() - last_inbound_scan > 60:
                        try:
                            reconcile(catalog, inbound_client)
                        except InboundError as exc:
                            logging.warning("Resend inbox reconciliation: %s", exc)
                        finally:
                            last_inbound_scan = time.monotonic()
                    if time.monotonic() - last_plan > 30:
                        plan(catalog)
                        last_plan = time.monotonic()
                    busy = deliver_one(catalog)
                    busy = forward_one(catalog, inbound_client) or busy
                elif args.mode == "collection-digest":
                    prepare_due_digest(catalog)
                    busy = deliver_digest(catalog, inbound_client)
                else:
                    busy = False
                stop.wait(0.1 if busy else 60 if args.mode == "collection-digest" else 2)
            except Exception:
                logging.exception("product worker iteration failed")
                stop.wait(5)
        health.shutdown()
