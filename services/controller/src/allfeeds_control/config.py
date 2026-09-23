from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from allfeeds_contracts import SourceSpec
from pydantic import BaseModel, ConfigDict


class SourcesFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    sources: tuple[SourceSpec, ...] = ()


@dataclass(frozen=True)
class LoadedConfig:
    path: Path
    value: SourcesFile
    version_hash: str


def load_sources(path: Path) -> LoadedConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    value = SourcesFile.model_validate(raw or {})
    ids = [source.id for source in value.sources]
    if len(ids) != len(set(ids)):
        raise ValueError("source ids must be unique")
    payload = json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return LoadedConfig(path, value, hashlib.sha256(payload.encode()).hexdigest()[:16])
