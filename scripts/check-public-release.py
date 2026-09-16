#!/usr/bin/env python3
"""Offline release hygiene check. Prints locations and rule names, never secret values.

Checks tracked working files; --history also checks every locally reachable Git blob.
This focused check is not a substitute for a full secret scanner or manual review.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

PRIVATE_PATH = re.compile(
    r"(^|/)(?:\.env(?:$|\.)|[^/]+\.env(?:$|\.)|\.deploy/|secrets/|backups/|private/|"
    r"identity\.json$|[^/]*(?:cookies|storage-state)[^/]*\.json$)|"
    r"\.(?:pem|key|p12|pfx|dump|sql\.gz|sqlite3?|bundle)$", re.I,
)
CREDENTIAL = re.compile(
    rb"(?<![A-Za-z0-9_])(?:sk-(?:proj-)?[A-Za-z0-9_-]{32,}|"
    rb"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|"
    rb"AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)


def private_path(name: str) -> bool:
    return bool(PRIVATE_PATH.search(name)) and not name.endswith(".env.example")


def known_secrets(paths: list[Path]) -> list[bytes]:
    values = set()
    for path in paths:
        for line in path.read_text().splitlines():
            key, separator, value = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
                continue
            value = value.strip().strip("\"'")
            if (
                re.search(r"PASSWORD|TOKEN|SECRET|API_KEY", key)
                and len(value) >= 12
                and not value.startswith(("change-me", "genchi-local", "test-"))
            ):
                values.update((value.encode(), quote(value, safe="").encode()))
    return sorted(values)


def findings(name: str, body: bytes, values: list[bytes]) -> list[str]:
    rules = []
    if private_path(name):
        rules.append("private-file")
    if CREDENTIAL.search(body):
        rules.append("credential-format")
    if any(value in body for value in values):
        rules.append("local-secret-value")
    return rules


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def scan(root: Path, history: bool, values: list[bytes]) -> int:
    count = failures = 0

    def check(label: str, name: str, body: bytes) -> None:
        nonlocal count, failures
        count += 1
        rules = findings(name, body, values)
        if rules:
            failures += 1
            print(f"{label}: {', '.join(rules)}")

    for raw in git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0"):
        if not raw:
            continue
        name = raw.decode()
        path = root / name
        if path.is_symlink():
            check(name, name, path.readlink().as_posix().encode())
        elif path.is_file():
            check(name, name, path.read_bytes())
    if history:
        objects = git(root, "rev-list", "--objects", "--all").splitlines()
        with subprocess.Popen(
            ["git", "-C", str(root), "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        ) as process:
            for entry in objects:
                oid, _, raw_name = entry.partition(b" ")
                process.stdin.write(oid + b"\n")
                process.stdin.flush()
                header = process.stdout.readline().split()
                body = process.stdout.read(int(header[2]))
                process.stdout.read(1)
                if header[1] == b"blob":
                    name = raw_name.decode()
                    check(f"{oid.decode()[:12]}:{name}", name, body)
            process.stdin.close()
            if process.wait():
                raise RuntimeError("git cat-file failed")
    print(f"Checked {count} file versions; {failures} findings. Secret values are not printed.")
    return int(bool(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--secrets-file", action="append", default=[], type=Path,
                        help="Compare local env secret values without printing them (repeatable)")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    return scan(root, args.history, known_secrets(args.secrets_file))


if __name__ == "__main__":
    raise SystemExit(main())
