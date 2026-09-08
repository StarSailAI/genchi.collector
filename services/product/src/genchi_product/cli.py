from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .importer import import_legacy
from .naming import normalize_catalog
from .notifications import deliver_one, plan
from .pipeline import index_raw, process_one
from .store import Catalog


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("import-legacy")
    sub.add_parser("plan")
    sub.add_parser("index-raw")
    sub.add_parser("refresh-structured")
    names = sub.add_parser("normalize-names", help="Preview/apply audited Chinese display names")
    names.add_argument("--apply", action="store_true")
    names.add_argument("--output", help="Write the complete reviewable report as JSON")
    worker = sub.add_parser("worker")
    worker.add_argument("--mode", choices=["catalog", "notifications"], default="catalog")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s"
    )
    catalog = Catalog()
    if args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(catalog), host="0.0.0.0", port=8080, access_log=False)
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
    else:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        state = {"heartbeat": time.monotonic()}

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                ok = time.monotonic() - state["heartbeat"] < 180
                self.send_response(200 if ok else 503)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": ok, "mode": args.mode}).encode())

            def log_message(self, *_):
                pass

        health = ThreadingHTTPServer(("0.0.0.0", 8070), Health)
        threading.Thread(target=health.serve_forever, daemon=True).start()
        last_plan = 0
        while not stop.is_set():
            try:
                state["heartbeat"] = time.monotonic()
                if args.mode == "catalog":
                    busy = process_one(catalog)
                else:
                    if time.monotonic() - last_plan > 30:
                        plan(catalog)
                        last_plan = time.monotonic()
                    busy = deliver_one(catalog)
                stop.wait(0.1 if busy else 2)
            except Exception:
                logging.exception("product worker iteration failed")
                stop.wait(5)
        health.shutdown()
