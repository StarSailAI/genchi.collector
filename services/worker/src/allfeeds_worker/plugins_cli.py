from __future__ import annotations

import argparse
import json
from importlib.metadata import entry_points
from pathlib import Path

import yaml
from allfeeds_contracts import SourceSpec


def _fetchers():
    plugins = [point.load()() for point in entry_points(group="allfeeds.fetchers")]
    return {plugin.manifest.name: plugin for plugin in plugins}


def main() -> None:
    parser = argparse.ArgumentParser(prog="allfeeds-plugin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    validate = sub.add_parser("validate")
    validate.add_argument("--sources", default="config/sources.yaml")
    args = parser.parse_args()
    fetchers = _fetchers()
    if args.command == "list":
        print(
            json.dumps(
                [
                    plugin.manifest.model_dump(mode="json")
                    for plugin in sorted(fetchers.values(), key=lambda item: item.manifest.name)
                ],
                indent=2,
            )
        )
        return
    raw = yaml.safe_load(Path(args.sources).read_text(encoding="utf-8")) or {}
    checked = 0
    errors = []
    for item in raw.get("sources") or ():
        source = SourceSpec.model_validate(item)
        plugin = fetchers.get(source.fetcher)
        if plugin is None:
            errors.append(
                {"source": source.id, "error": f"fetcher not installed: {source.fetcher}"}
            )
            continue
        try:
            plugin.validate(source.config)
            checked += 1
        except Exception as exc:
            errors.append({"source": source.id, "error": f"{type(exc).__name__}: {exc}"})
    print(json.dumps({"valid": not errors, "checked": checked, "errors": errors}, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
