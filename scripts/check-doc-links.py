#!/usr/bin/env python3
"""Check local Markdown links, images and heading anchors without network access."""

from __future__ import annotations

import re
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def prose(text: str) -> str:
    lines = []
    fence = None
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            marker = match[1]
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            lines.append("")
        else:
            lines.append(line if fence is None else "")
    return "\n".join(lines)


def anchors(text: str) -> set[str]:
    result = set(re.findall(r'\b(?:id|name)=["\x27]([^"\x27]+)["\x27]', text))
    for title in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", prose(text), re.M):
        title = re.sub(r"<[^>]+>", "", title).lower().replace("`", "")
        slug = "".join(c for c in title if c in "-_ " or unicodedata.category(c)[0] in "LN")
        slug = slug.replace(" ", "-")
        candidate, suffix = slug, 0
        while candidate in result:
            suffix += 1
            candidate = f"{slug}-{suffix}"
        result.add(candidate)
    return result


def main() -> int:
    names = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    ).decode().split("\0")
    checked = errors = files = 0
    for name in sorted(set(names)):
        path = ROOT / name
        if not name.endswith(".md") or not path.is_file():
            continue
        files += 1
        text = prose(path.read_text())
        links = list(re.finditer(r'!?\[[^\]\n]*\]\(<?([^\s)>]+)>?(?:\s+"[^"]*")?\)', text))
        links += list(re.finditer(r'^\s*\[[^\]]+\]:\s*<?([^\s>]+)>?', text, re.M))
        links += list(re.finditer(r'<(?:img|a)\b[^>]*?\b(?:src|href)="([^"]+)"', text))
        for match in links:
            target = urlsplit(match[1])
            if target.scheme or target.netloc:
                continue
            checked += 1
            destination = (path.parent / unquote(target.path)).resolve() if target.path else path
            reason = None
            if not destination.is_relative_to(ROOT):
                reason = "link leaves repository"
            elif not destination.exists():
                reason = "missing file"
            elif target.fragment and destination.suffix == ".md":
                if unquote(target.fragment) not in anchors(destination.read_text()):
                    reason = "missing heading anchor"
            if reason:
                line = text.count("\n", 0, match.start()) + 1
                print(f"{name}:{line}: {reason}: {match[1]}")
                errors += 1
    print(f"Checked {checked} local links in {files} Markdown files; {errors} errors.")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
