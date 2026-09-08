#!/usr/bin/env python3
"""Create/reuse this site's Resend webhook and save its secret in the shared env."""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from manage import private_write, read_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    path = args.root / ".env"
    values = read_env(path)
    key = values.get("RESEND_API_KEY", "")
    if not key or not values.get("ADMIN_EMAIL"):
        raise ValueError("Fill RESEND_API_KEY (Full access) and ADMIN_EMAIL first")
    site = values.get("PUBLIC_SITE_URL", "").rstrip("/")
    if not site.startswith("https://"):
        raise ValueError("PUBLIC_SITE_URL must use HTTPS")
    endpoint = site + "/api/webhooks/resend"

    def api(method, route, payload=None):
        time.sleep(0.65)
        request = urllib.request.Request(
            "https://api.resend.com" + route,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "User-Agent": "Genchi inbound setup",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise ValueError(
                f"Resend returned HTTP {exc.code}; inbound setup requires a Full access key"
            ) from None
        except urllib.error.URLError:
            raise ValueError("Unable to reach Resend API") from None

    # Verify permissions before modifying anything in the Resend account.
    api("GET", "/emails/receiving?limit=1")
    hooks = api("GET", "/webhooks")
    if hooks.get("has_more"):
        raise ValueError("Webhook list is paginated; inspect existing endpoints before adding one")
    matching = [hook for hook in hooks.get("data", []) if hook.get("endpoint") == endpoint]
    if len(matching) > 1:
        raise ValueError("Duplicate site webhooks found; consolidate them before setup")
    if matching:
        hook = api("GET", "/webhooks/" + matching[0]["id"])
        if hook.get("status") != "enabled" or hook.get("events") != ["email.received"]:
            api(
                "PATCH",
                "/webhooks/" + hook["id"],
                {"events": ["email.received"], "status": "enabled"},
            )
    else:
        hook = api("POST", "/webhooks", {"endpoint": endpoint, "events": ["email.received"]})
    secret = hook.get("signing_secret") or values.get("RESEND_WEBHOOK_SECRET")
    if not secret or not re.fullmatch(r"whsec_[A-Za-z0-9+/=_-]+", secret):
        raise ValueError(
            "Copy the existing webhook signing secret to RESEND_WEBHOOK_SECRET, then rerun"
        )
    # Re-read immediately before changing only the managed field; preserve user edits.
    text = path.read_text()
    line = "RESEND_WEBHOOK_SECRET=" + secret
    if re.search(r"(?m)^RESEND_WEBHOOK_SECRET=", text):
        text = re.sub(r"(?m)^RESEND_WEBHOOK_SECRET=.*$", lambda _: line, text)
    else:
        text += "\n" + line + "\n"
    private_write(path, text)
    print(json.dumps({"endpoint": endpoint, "webhook_id": hook["id"], "secret_saved": True}))
    print(
        "Run deploy/manage.py apply, then compose backend exec -T notifier genchi-product inbound-backfill."
    )


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
