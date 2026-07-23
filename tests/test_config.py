from __future__ import annotations

from pathlib import Path

from allfeeds_control.config import load_sources


def test_example_config_is_valid() -> None:
    path = Path(__file__).parents[1] / "config" / "sources.yaml"
    loaded = load_sources(path)
    assert len(loaded.value.sources) == 12
    assert all(source.enabled for source in loaded.value.sources)
    assert loaded.version_hash
