#!/usr/bin/env python3
"""Create local-only configuration without starting services or replacing secrets."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

KEYS = {
    "POSTGRES_PASSWORD", "GENCHI_READER_PASSWORD", "CONTROL_API_TOKEN",
    "ENROLLMENT_TOKEN", "BROWSER_API_TOKEN", "PRODUCT_SECRET",
    "PRODUCT_PROXY_SECRET", "PRODUCT_ADMIN_TOKEN",
}


def initialize(root: Path) -> None:
    lines = []
    for line in (root / ".env.example").read_text().splitlines():
        key, separator, _ = line.partition("=")
        lines.append(f"{key}={secrets.token_hex(32)}" if separator and key in KEYS else line)
    # O_EXCL also rejects symlinks and protects an existing installation.
    fd = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(lines) + "\n")
    print("Created .env (0600). Local credentials generated; external integrations left empty.")


if __name__ == "__main__":
    try:
        initialize(Path(__file__).resolve().parents[1])
    except FileExistsError:
        raise SystemExit(".env already exists; existing configuration preserved.") from None
