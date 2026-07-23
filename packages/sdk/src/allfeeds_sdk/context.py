from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from allfeeds_contracts import ResourceAsset, ResourceRecord


@dataclass(frozen=True)
class FetchRequest:
    task_id: int
    source_id: str
    operation: str
    config: Mapping[str, Any]
    tags: tuple[str, ...]
    window_start: datetime | None = None
    window_end: datetime | None = None


class FetchContext:
    """Runtime services exposed to plugins without exposing controller internals."""

    def __init__(
        self,
        *,
        emit_record: Callable[[ResourceRecord], None],
        emit_asset: Callable[[ResourceAsset, bytes | None], None],
        load_checkpoint: Callable[[], dict[str, Any]],
        save_checkpoint: Callable[[dict[str, Any]], None],
        secret_provider: Callable[[str], str | None],
        logger: Any,
    ):
        self._emit_record = emit_record
        self._emit_asset = emit_asset
        self._load_checkpoint = load_checkpoint
        self._save_checkpoint = save_checkpoint
        self._secret_provider = secret_provider
        self.logger = logger
        self.seen = 0
        self.assets = 0
        self.content_characters = 0
        self.bytes_downloaded = 0

    def emit(self, record: ResourceRecord) -> None:
        self._emit_record(record)
        self.seen += 1
        self.content_characters += len(record.content or "")

    def emit_asset(self, asset: ResourceAsset, content: bytes | None = None) -> None:
        self._emit_asset(asset, content)
        self.assets += 1
        self.bytes_downloaded += len(content or b"")

    def checkpoint(self) -> dict[str, Any]:
        return dict(self._load_checkpoint() or {})

    def set_checkpoint(self, value: dict[str, Any]) -> None:
        self._save_checkpoint(dict(value))

    def secret(self, name: str, *, required: bool = True) -> str | None:
        value = self._secret_provider(name)
        if required and not value:
            from .exceptions import ConfigurationError

            raise ConfigurationError(f"required secret {name!r} is not configured")
        return value
