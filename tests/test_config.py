from pathlib import Path

from allfeeds_control.config import load_sources


def test_example_sources_are_valid_and_disabled() -> None:
    path = Path(__file__).parents[1] / "config" / "sources.yaml"
    loaded = load_sources(path)
    assert {source.id for source in loaded.value.sources} == {
        "example-rss",
        "example-official-page",
    }
    assert all(not source.enabled for source in loaded.value.sources)
    assert loaded.version_hash
