#!/usr/bin/env python3
"""Single-host deployment with a shared, private env file. Python stdlib only."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

INTERNAL_KEYS = (
    "POSTGRES_PASSWORD",
    "GENCHI_READER_PASSWORD",
    "CONTROL_API_TOKEN",
    "ENROLLMENT_TOKEN",
    "BROWSER_API_TOKEN",
    "PRODUCT_SECRET",
    "PRODUCT_ADMIN_TOKEN",
)


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"Invalid env assignment on line {number}")
        if key in values:
            raise ValueError(f"Duplicate env key: {key}")
        if value.startswith("'"):
            if not value.endswith("'") or len(value) < 2:
                raise ValueError(f"Unclosed quote on line {number}")
            value = value[1:-1].replace("\\'", "'")
        elif value.startswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise ValueError(f"Invalid quoted value on line {number}") from None
        else:
            value = value.split(" #", 1)[0].rstrip()
        if "\n" in value or "\r" in value or "\0" in value:
            raise ValueError(f"Multiline values are unsupported: {key}")
        values[key] = value
    return values


def private_write(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(text)
    path.chmod(0o600)


def migrate_browser_env(path: Path) -> None:
    """Preserve the existing browser credential while retiring engine-specific options."""
    values = read_env(path)
    old_keys = [key for key in values if key.startswith("CLOAKBROWSER_")]
    if not old_keys:
        print("Browser env already uses the current configuration.")
        return
    directory = path.parent / ".deploy"
    directory.mkdir(mode=0o700, exist_ok=True)
    backup_path = directory / "env-before-camoufox"
    if not backup_path.exists():
        private_write(backup_path, path.read_text())
    retired = {"IMAGE", "LICENSE_KEY", "FINGERPRINT_SEED", "PROFILE_DIR", "HUMANIZE", "AUTO_UPDATE"}
    lines = []
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().startswith("CLOAKBROWSER_"):
            suffix = key.strip().removeprefix("CLOAKBROWSER_")
            current = "BROWSER_" + suffix
            if suffix in retired or current in values:
                continue
            if suffix == "URL":
                value = value.replace("http://cloakbrowser:3003", "http://browser:3003")
            line = current + "=" + value
        lines.append(line)
    private_write(path, "\n".join(lines) + "\n")
    print("Browser env migrated; existing credentials preserved, previous env backed up privately.")


def init_env(root: Path, site: str) -> None:
    path = root / ".env"
    if path.exists():
        raise ValueError(f"Already exists; existing credentials preserved: {path}")
    text = (Path(__file__).parent / "production.env.example").read_text()
    text = text.replace("PUBLIC_SITE_URL=http://localhost", f"PUBLIC_SITE_URL={site}")
    for key in INTERNAL_KEYS:
        text = text.replace(f"\n{key}=\n", f"\n{key}={secrets.token_hex(32)}\n")
    private_write(path, text)
    print(f"Created {path} (0600); internal passwords generated, external credentials left empty.")


def prepare(root: Path) -> dict[str, str]:
    path = root / ".env"
    values = read_env(path)
    path.chmod(0o600)
    for key in INTERNAL_KEYS:
        if len(values.get(key, "")) < 32:
            raise ValueError(f"Missing or short internal secret: {key}")
    site = values.get("PUBLIC_SITE_URL", "").rstrip("/")
    url = urlsplit(site)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.path
        or url.query
        or url.fragment
        or url.port
    ):
        raise ValueError("PUBLIC_SITE_URL must be http(s)://host with standard ports and no path")
    values["PUBLIC_SITE_URL"] = site
    llm = [bool(values.get(key)) for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")]
    if any(llm) and not all(llm):
        raise ValueError(
            "Fill LLM_BASE_URL, LLM_API_KEY and LLM_MODEL together, or leave all empty"
        )
    values["CATALOG_WORKER_MODE"] = "catalog" if all(llm) else "idle"
    provider = values.get("MAIL_PROVIDER", "resend")
    if provider not in {"resend", "smtp"}:
        raise ValueError("MAIL_PROVIDER must be resend or smtp")
    if provider == "resend":
        values.update(
            SMTP_HOST="smtp.resend.com",
            SMTP_PORT="465",
            SMTP_SECURITY="ssl",
            SMTP_USER="resend",
            SMTP_PASSWORD=values.get("RESEND_API_KEY", ""),
        )
        values["NOTIFIER_WORKER_MODE"] = "notifications" if values["SMTP_PASSWORD"] else "idle"
    else:
        values["NOTIFIER_WORKER_MODE"] = "notifications"
    if (
        values["NOTIFIER_WORKER_MODE"] == "notifications"
        and values.get("SMTP_HOST", "mailpit") != "mailpit"
    ):
        if values.get("SMTP_SECURITY") not in {"ssl", "starttls"}:
            raise ValueError("External SMTP requires SMTP_SECURITY=ssl or starttls")
        if not 1 <= int(values.get("SMTP_PORT", "0")) <= 65535:
            raise ValueError("Invalid SMTP_PORT")
        if values.get("SMTP_USER") and not values.get("SMTP_PASSWORD"):
            raise ValueError("SMTP_USER requires SMTP_PASSWORD")
        if not values.get("MAIL_FROM") or "genchi.local" in values["MAIL_FROM"]:
            raise ValueError("Set an authorized MAIL_FROM before enabling external SMTP")
    runtime, secret_dir = root / ".deploy", root / "secrets"
    for directory in (runtime, secret_dir):
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    enable_x = values.get("ENABLE_X", "false").lower() == "true"
    cookies = []
    if enable_x:
        cookie_path = Path(values.get("X_COOKIES_FILE", "")).expanduser()
        if not cookie_path.is_file() or cookie_path.stat().st_size > 131072:
            raise ValueError(
                "ENABLE_X=true requires a Cookie JSON file in X_COOKIES_FILE (max 128KB)"
            )
        cookies = json.loads(cookie_path.read_text())
        if (
            not isinstance(cookies, list)
            or not cookies
            or not all(isinstance(c, dict) for c in cookies)
        ):
            raise ValueError("X_COOKIES_FILE must contain a nonempty Playwright cookies array")
    cookie_target = secret_dir / "x-cookies.json"
    if enable_x or not cookie_target.exists():
        private_write(cookie_target, json.dumps(cookies))
    # The parent remains 0700 on the host. Bind only this file into the non-root
    # browser container; no other secret directory contents become accessible.
    cookie_target.chmod(0o644)
    source = (root / "genchi.collector/config/sources.yaml").read_text()
    if not enable_x:
        parts = re.split(r"(?m)(?=^  - id: )", source)
        source = "".join(
            re.sub(r"(?m)^    enabled: true$", "    enabled: false", part)
            if "\n    fetcher: genchi.x_profile\n" in part
            else part
            for part in parts
        )
    private_write(runtime / "sources.yaml", source)
    values["GENCHI_ROOT"] = str(root)
    values["DATABASE_URL"] = (
        "postgresql://genchi_reader:"
        + quote(values["GENCHI_READER_PASSWORD"], safe="")
        + "@postgres:5432/genchi?schema=genchi"
    )
    values["NEXT_PUBLIC_SITE_URL"] = site
    env_lines = []
    for key, value in values.items():
        env_lines.append(key + "='" + value.replace("'", "\\'") + "'")
    private_write(runtime / "effective.env", "\n".join(env_lines) + "\n")
    return values


def compose(root: Path, target: str, args: list[str], **kwargs):
    docker = ["docker"] if os.geteuid() == 0 else ["sudo", "-n", "docker"]
    repo = root / ("genchi.news" if target == "web" else "genchi.collector")
    override = (
        root
        / "genchi.collector/deploy"
        / ("compose.web.yml" if target == "web" else "compose.production.yml")
    )
    return subprocess.run(
        docker
        + [
            "compose",
            "--parallel",
            "1",
            "--env-file",
            str(root / ".deploy/effective.env"),
            "-f",
            str(repo / "docker-compose.yml"),
            "-f",
            str(override),
        ]
        + args,
        check=True,
        **kwargs,
    )


def backup(root: Path, values: dict[str, str]) -> None:
    directory = root / "backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    now = datetime.now(timezone.utc)
    target = directory / f"genchi-{now:%Y%m%dT%H%M%SZ}.dump"
    temporary = target.with_suffix(".partial")
    with temporary.open("xb") as output:
        temporary.chmod(0o600)
        compose(
            root,
            "backend",
            [
                "exec",
                "-T",
                "postgres",
                "pg_dump",
                "-U",
                "genchi",
                "-d",
                "genchi",
                "-Fc",
                "--no-owner",
            ],
            stdout=output,
        )
    with temporary.open("rb") as input_file:
        compose(
            root,
            "backend",
            ["exec", "-T", "postgres", "pg_restore", "--list"],
            stdin=input_file,
            stdout=subprocess.DEVNULL,
        )
    temporary.rename(target)
    keep = max(1, int(values.get("BACKUP_KEEP_DAYS", "7")))
    for old in directory.glob("genchi-*.dump"):
        if old != target and old.stat().st_mtime < (now - timedelta(days=keep)).timestamp():
            old.unlink()
    print(f"Verified backup: {target}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--site-url", required=True)
    for name in ("check", "apply", "status", "backup", "migrate-browser-env"):
        sub.add_parser(name)
    raw = sub.add_parser("compose", help="Forward arguments to backend or web Compose")
    raw.add_argument("target", choices=["backend", "web"])
    raw.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "migrate-browser-env":
        migrate_browser_env(root / ".env")
        return
    if args.command == "init":
        init_env(root, args.site_url)
        return
    effective = root / ".deploy/effective.env"
    values = (
        read_env(effective)
        if args.command in {"status", "backup"} and effective.exists()
        else prepare(root)
    )
    if args.command == "check":
        for target in ("backend", "web"):
            compose(root, target, ["config", "--quiet"])
        print("Configuration valid; no secret values printed.")
        print("Catalog worker mode:", values["CATALOG_WORKER_MODE"])
        print(
            "Email:",
            "paused; fill RESEND_API_KEY"
            if values["NOTIFIER_WORKER_MODE"] == "idle"
            else "Resend SMTP/TLS"
            if values.get("MAIL_PROVIDER", "resend") == "resend"
            else "private Mailpit capture"
            if values["SMTP_HOST"] == "mailpit"
            else "external SMTP",
        )
        print(
            "X collection:",
            "enabled" if values.get("ENABLE_X", "false").lower() == "true" else "disabled",
        )
        print(
            "Inbound forwarding:",
            "configured"
            if values.get("RESEND_WEBHOOK_SECRET")
            else "not configured; run deploy/inbound-setup.py after enabling receiving",
        )
        if not values.get("ADMIN_EMAIL"):
            print("Pending: ADMIN_EMAIL")
    elif args.command == "apply":
        compose(
            root,
            "backend",
            [
                "up",
                "-d",
                "--no-build",
                "--wait",
                "--wait-timeout",
                "240",
                "postgres",
                "control",
                "browser",
                "worker",
                "normalizer",
                "product",
                "mailpit",
                "notifier",
            ],
        )
        compose(root, "web", ["up", "-d", "--no-build", "--wait", "--wait-timeout", "120", "web"])
        if shutil.which("nginx"):
            privileged = [] if os.geteuid() == 0 else ["sudo", "-n"]
            subprocess.run(privileged + ["nginx", "-t"], check=True)
            subprocess.run(privileged + ["systemctl", "reload", "nginx"], check=True)
        else:
            print(
                "Application services ready on loopback; configure host Nginx before public access."
            )
    elif args.command == "status":
        for target in ("backend", "web"):
            compose(root, target, ["ps"])
    elif args.command == "backup":
        backup(root, values)
    else:
        compose(root, args.target, args.args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from None
