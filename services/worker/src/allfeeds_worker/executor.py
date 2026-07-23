from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from allfeeds_contracts import FetchReport, ResourceAsset, ResourceRecord, SourceSpec, TaskEnvelope
from allfeeds_sdk import FetchContext, FetchRequest, PluginRegistry

from .assets import LocalAssetStore
from .sink import PostgresSink

logger = logging.getLogger(__name__)


class TaskExecutor:
    def __init__(self, *, asset_path: Path, registry: PluginRegistry | None = None):
        self.asset_path = asset_path
        self.registry = registry or PluginRegistry.discover()
        if "postgres" not in self.registry.sinks:
            self.registry.add_sink(PostgresSink())
        if "local" not in self.registry.asset_stores:
            self.registry.add_asset_store(LocalAssetStore(asset_path))

    def execute(self, task: TaskEnvelope) -> FetchReport:
        source = SourceSpec.model_validate(task.source)
        fetcher = self.registry.fetchers.get(source.fetcher)
        if fetcher is None:
            raise RuntimeError(f"fetcher plugin is not installed: {source.fetcher}")
        if task.operation not in fetcher.manifest.operations:
            raise RuntimeError(
                f"fetcher {source.fetcher} does not support operation {task.operation}"
            )
        sink = self.registry.sinks.get(source.sink)
        if sink is None:
            raise RuntimeError(f"sink plugin is not installed: {source.sink}")
        asset_store_name = source.asset_store or os.environ.get("DEFAULT_ASSET_STORE", "local")
        asset_store = self.registry.asset_stores.get(asset_store_name)
        if source.asset_store and asset_store is None:
            raise RuntimeError(f"asset store plugin is not installed: {source.asset_store}")
        validated = fetcher.validate(source.config)
        records: list[ResourceRecord] = []
        assets: list[ResourceAsset] = []
        sink_counts = {"added": 0, "updated": 0, "duplicate": 0}
        pending_checkpoint = (
            sink.load_checkpoint(source.id) if hasattr(sink, "load_checkpoint") else {}
        )

        def flush_records() -> None:
            if not records:
                return
            counts = sink.write_records(source.id, list(records))
            for key in sink_counts:
                sink_counts[key] += int(counts.get(key, 0))
            records.clear()

        def emit_record(record: ResourceRecord) -> None:
            records.append(record)
            if len(records) >= 100:
                flush_records()

        def emit_asset(asset: ResourceAsset, content: bytes | None) -> None:
            if content is not None:
                if asset_store is None:
                    raise RuntimeError("asset bytes were emitted without an asset store")
                asset = asset_store.store(source.id, asset, content)
            assets.append(asset)
            if len(assets) >= 100:
                sink.write_assets(source.id, list(assets))
                assets.clear()

        def load_checkpoint() -> dict[str, Any]:
            return dict(pending_checkpoint)

        def save_checkpoint(value: dict[str, Any]) -> None:
            pending_checkpoint.clear()
            pending_checkpoint.update(value)

        context = FetchContext(
            emit_record=emit_record,
            emit_asset=emit_asset,
            load_checkpoint=load_checkpoint,
            save_checkpoint=save_checkpoint,
            secret_provider=lambda name: os.environ.get(name),
            logger=logger,
        )
        request = FetchRequest(
            task_id=task.id,
            source_id=source.id,
            operation=task.operation,
            config=validated.model_dump(mode="python"),
            tags=source.tags,
            window_start=task.window_start,
            window_end=task.window_end,
        )
        plugin_report = (
            fetcher.backfill(context, request)
            if task.operation == "backfill"
            else fetcher.fetch(context, request)
        ) or FetchReport()
        flush_records()
        if assets:
            sink.write_assets(source.id, list(assets))
        if hasattr(sink, "save_checkpoint"):
            sink.save_checkpoint(source.id, pending_checkpoint)
        return plugin_report.model_copy(
            update={
                "seen": max(plugin_report.seen, context.seen),
                "added": sink_counts["added"],
                "updated": sink_counts["updated"],
                "duplicate": sink_counts["duplicate"],
                "assets": max(plugin_report.assets, context.assets),
                "content_characters": max(
                    plugin_report.content_characters, context.content_characters
                ),
                "bytes_downloaded": max(plugin_report.bytes_downloaded, context.bytes_downloaded),
                "checkpoint": pending_checkpoint or None,
            }
        )
