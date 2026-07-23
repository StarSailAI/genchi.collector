from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from importlib.metadata import entry_points
from typing import Any, Literal

from allfeeds_contracts import FetchReport, PluginInventory, ResourceAsset, ResourceRecord
from pydantic import BaseModel, ConfigDict, Field

from .context import FetchContext, FetchRequest


class FetcherManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,190}$")
    version: str
    api_version: Literal["1"] = "1"
    operations: tuple[Literal["fetch", "backfill"], ...] = ("fetch",)
    default_queue: str = "default"
    default_timeout_seconds: int = 300
    capabilities: tuple[str, ...] = ()

    def inventory(self) -> PluginInventory:
        return PluginInventory(
            name=self.name,
            kind="fetcher",
            version=self.version,
            api_version=self.api_version,
            capabilities=self.capabilities,
            operations=self.operations,
            default_queue=self.default_queue,
        )


class FetcherPlugin(ABC):
    manifest: FetcherManifest
    config_model: type[BaseModel]

    def validate(self, config: dict[str, Any]) -> BaseModel:
        return self.config_model.model_validate(config)

    @abstractmethod
    def fetch(
        self,
        context: FetchContext,
        request: FetchRequest,
    ) -> FetchReport | None:
        raise NotImplementedError

    def backfill(
        self,
        context: FetchContext,
        request: FetchRequest,
    ) -> FetchReport | None:
        return self.fetch(context, request)


class SinkPlugin(ABC):
    name: str
    version: str = "0.1.0"
    api_version: str = "1"

    @abstractmethod
    def write_records(self, source_id: str, records: list[ResourceRecord]) -> dict[str, int]:
        raise NotImplementedError

    @abstractmethod
    def write_assets(self, source_id: str, assets: list[ResourceAsset]) -> int:
        raise NotImplementedError

    def inventory(self) -> PluginInventory:
        return PluginInventory(
            name=self.name,
            kind="sink",
            version=self.version,
            api_version=self.api_version,
        )


class AssetStorePlugin(ABC):
    name: str
    version: str = "0.1.0"
    api_version: str = "1"

    @abstractmethod
    def store(self, source_id: str, asset: ResourceAsset, content: bytes) -> ResourceAsset:
        raise NotImplementedError

    def inventory(self) -> PluginInventory:
        return PluginInventory(
            name=self.name,
            kind="asset_store",
            version=self.version,
            api_version=self.api_version,
        )


class PluginRegistry:
    def __init__(
        self,
        fetchers: Iterable[FetcherPlugin] = (),
        sinks: Iterable[SinkPlugin] = (),
        asset_stores: Iterable[AssetStorePlugin] = (),
    ):
        self.fetchers = self._index(fetchers, lambda plugin: plugin.manifest.name, "fetcher")
        self.sinks = self._index(sinks, lambda plugin: plugin.name, "sink")
        self.asset_stores = self._index(asset_stores, lambda plugin: plugin.name, "asset store")

    @staticmethod
    def _index(plugins, key, kind):
        values = {}
        for plugin in plugins:
            name = key(plugin)
            if name in values:
                raise ValueError(f"duplicate {kind} plugin: {name}")
            values[name] = plugin
        return values

    @classmethod
    def discover(cls) -> PluginRegistry:
        fetchers = [point.load()() for point in entry_points(group="allfeeds.fetchers")]
        sinks = [point.load()() for point in entry_points(group="allfeeds.sinks")]
        asset_stores = [point.load()() for point in entry_points(group="allfeeds.asset_stores")]
        return cls(fetchers, sinks, asset_stores)

    def add_fetcher(self, plugin: FetcherPlugin) -> None:
        if plugin.manifest.name in self.fetchers:
            raise ValueError(f"duplicate fetcher plugin: {plugin.manifest.name}")
        self.fetchers[plugin.manifest.name] = plugin

    def add_sink(self, plugin: SinkPlugin) -> None:
        if plugin.name in self.sinks:
            raise ValueError(f"duplicate sink plugin: {plugin.name}")
        self.sinks[plugin.name] = plugin

    def add_asset_store(self, plugin: AssetStorePlugin) -> None:
        if plugin.name in self.asset_stores:
            raise ValueError(f"duplicate asset store plugin: {plugin.name}")
        self.asset_stores[plugin.name] = plugin

    def inventories(self) -> list[PluginInventory]:
        values = [plugin.manifest.inventory() for plugin in self.fetchers.values()]
        values.extend(plugin.inventory() for plugin in self.sinks.values())
        values.extend(plugin.inventory() for plugin in self.asset_stores.values())
        return sorted(values, key=lambda item: (item.kind, item.name))
