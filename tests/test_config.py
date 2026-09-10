from __future__ import annotations

from pathlib import Path

from allfeeds_control.config import load_sources


def test_example_config_is_valid() -> None:
    path = Path(__file__).parents[1] / "config" / "sources.yaml"
    loaded = load_sources(path)
    existing = [s for s in loaded.value.sources if s.fetcher != "genchi.aggregator"]
    assert len(existing) == 13 and all(s.enabled for s in existing)
    assert {"collabo-cafe-news", "anime-hack-events"} <= {s.id for s in loaded.value.sources}
    assert loaded.version_hash
