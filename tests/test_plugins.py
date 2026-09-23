from __future__ import annotations

from allfeeds_sdk import PluginRegistry
from example_fetcher import ExampleFetcher


def test_registry_inventory_contains_fetcher_capability() -> None:
    registry = PluginRegistry(fetchers=[ExampleFetcher()])
    inventory = registry.inventories()[0]
    assert inventory.capability == "fetcher:example.hello:api-v1"
    assert "backfill" in inventory.operations


def test_duplicate_plugin_is_rejected() -> None:
    registry = PluginRegistry(fetchers=[ExampleFetcher()])
    try:
        registry.add_fetcher(ExampleFetcher())
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate plugin was accepted")
