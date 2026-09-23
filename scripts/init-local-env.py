#!/usr/bin/env python3
"""Create local credentials without replacing an existing .env file."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

KEYS = {"POSTGRES_PASSWORD", "CONTROL_API_TOKEN", "ENROLLMENT_TOKEN"}


def initialize(root: Path) -> None:
    lines = []
    for line in (root / ".env.example").read_text().splitlines():
        key, separator, _ = line.partition("=")
        lines.append(f"{key}={secrets.token_hex(32)}" if separator and key in KEYS else line)
    fd = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(lines) + "\n")
    print("Created local .env (0600). No services or collection started.")


if __name__ == "__main__":
    try:
        initialize(Path(__file__).resolve().parents[1])
    except FileExistsError:
        raise SystemExit(".env already exists; existing configuration preserved.") from None
