#!/usr/bin/env python3
"""Send repository history to a host that cannot reach the Git provider."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

REPOSITORIES = ("genchi.news", "genchi.collector")


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="SSH target, for example ubuntu@server")
    parser.add_argument("--remote-root", default="/home/ubuntu/genchi")
    parser.add_argument("--branch", required=True, help="Branch to sync in both repositories")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", args.host):
        raise ValueError("Invalid SSH host")
    remote_root = Path(args.remote_root)
    if not remote_root.is_absolute() or ".." in remote_root.parts:
        raise ValueError("Remote root must be an absolute normalized path")

    workspace = Path(__file__).resolve().parents[2]
    expected: dict[str, str] = {}
    remote_mirror = remote_root / ".deploy/git-mirrors"
    run(["ssh", args.host, "mkdir -p " + shlex.quote(str(remote_mirror))])
    with tempfile.TemporaryDirectory(prefix="genchi-git-bundles-") as temporary:
        bundles: list[Path] = []
        for name in REPOSITORIES:
            repository = workspace / name
            if not (repository / ".git").is_dir():
                raise FileNotFoundError(f"Git repository not found: {repository}")
            expected[name] = subprocess.check_output(
                ["git", "-C", str(repository), "rev-parse", args.branch], text=True
            ).strip()
            bundle = Path(temporary) / f"{name}.bundle"
            run(["git", "-C", str(repository), "bundle", "create", str(bundle), args.branch])
            run(["git", "-C", str(repository), "bundle", "verify", str(bundle)])
            bundles.append(bundle)
        run(["scp", *(str(bundle) for bundle in bundles), f"{args.host}:{remote_mirror}/"])

    commands = ["set -eu"]
    for name in REPOSITORIES:
        repository = remote_root / name
        bundle = remote_mirror / f"{name}.bundle"
        repo = shlex.quote(str(repository))
        source = shlex.quote(str(bundle))
        commands.extend(
            [
                f"git -C {repo} bundle verify {source} >/dev/null",
                (
                    f"if git -C {repo} remote get-url deploy-bundle >/dev/null 2>&1; "
                    f"then git -C {repo} remote set-url deploy-bundle {source}; "
                    f"else git -C {repo} remote add deploy-bundle {source}; fi"
                ),
                f"git -C {repo} fetch deploy-bundle",
                (
                    f"test \"$(git -C {repo} rev-parse "
                    f"refs/remotes/deploy-bundle/{shlex.quote(args.branch)})\" = "
                    f"{shlex.quote(expected[name])}"
                ),
                f"printf '%s %s\\n' {shlex.quote(name)} {shlex.quote(expected[name])}",
            ]
        )
    run(["ssh", args.host, "\n".join(commands)])


if __name__ == "__main__":
    main()
