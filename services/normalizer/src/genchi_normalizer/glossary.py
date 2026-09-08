"""One versioned editorial glossary, shared by every extraction prompt."""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files


@cache
def load_glossary() -> dict:
    value = json.loads(files("genchi_normalizer").joinpath("data/glossary.json").read_text())
    if not value.get("version") or not isinstance(value.get("terms"), dict):
        raise ValueError("Invalid editorial glossary")
    if any(
        not k.strip() or not isinstance(v, str) or not v.strip() for k, v in value["terms"].items()
    ):
        raise ValueError("Glossary aliases and preferred names must be nonempty strings")
    return value


def glossary_prompt() -> str:
    value = load_glossary()
    return (
        "\nGenchi editorial glossary (trusted configuration), version " + value["version"] + ":\n"
        "Translate descriptive names into natural Simplified Chinese. 'terms' maps aliases to their "
        "preferred spellings; 'protected' names must not be translated literally; 'subjects' fixes each "
        "series' preferred name. Keep the source title separately. Do not concatenate translated and "
        "original copies. Preserve dates, edition numbers, membership tiers, seats and ticket types. "
        "An unknown proper name stays in its original form for review. Do not infer event identity "
        "or activity relevance merely because a glossary term matches.\n"
        + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        + "\nEnd of editorial glossary.\n"
    )
