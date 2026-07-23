from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOL_VERSION = "1.0"
API_VERSION = "v1"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScheduleSpec(ContractModel):
    type: Literal["interval", "cron", "once"] = "interval"
    seconds: int | None = Field(default=None, ge=10)
    cron: str | None = None
    at: datetime | None = None
    timezone: str = "UTC"

    @model_validator(mode="after")
    def validate_variant(self) -> ScheduleSpec:
        required = {
            "interval": self.seconds,
            "cron": self.cron,
            "once": self.at,
        }[self.type]
        if required is None or required == "":
            raise ValueError(f"schedule type {self.type!r} is missing its value")
        return self


class RetrySpec(ContractModel):
    max_attempts: int = Field(default=3, ge=1, le=100)
    backoff: Literal["fixed", "exponential"] = "exponential"
    base_seconds: int = Field(default=30, ge=0, le=86_400)
    max_seconds: int = Field(default=3600, ge=1, le=604_800)


class RoutingSpec(ContractModel):
    queue: str = "default"
    slot_cost: int = Field(default=1, ge=1, le=128)
    capabilities: tuple[str, ...] = ()
    resources: dict[str, int] = Field(default_factory=dict)
    preferred_worker: str | None = None

    @field_validator("resources")
    @classmethod
    def validate_resource_limits(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not key or amount < 1 for key, amount in value.items()):
            raise ValueError("resource names must be non-empty and amounts positive")
        return value


class SourceSpec(ContractModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
    enabled: bool = True
    fetcher: str = Field(min_length=1, max_length=191)
    schedule: ScheduleSpec
    config: dict[str, Any] = Field(default_factory=dict)
    sink: str = "postgres"
    asset_store: str | None = None
    priority: int = Field(default=20, ge=0, le=999)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    retry: RetrySpec = Field(default_factory=RetrySpec)
    routing: RoutingSpec = Field(default_factory=RoutingSpec)
    tags: tuple[str, ...] = ()
    backfill_enabled: bool = False
    backfill_window_seconds: int = Field(default=86_400, ge=60)


class TaskEnvelope(ContractModel):
    id: int
    dedupe_key: str
    operation: Literal["fetch", "backfill", "maintenance"]
    workload: Literal["scheduled", "manual", "backfill", "maintenance"]
    source_id: str
    source: dict[str, Any]
    priority: int
    status: str
    scheduled_for: datetime
    not_before: datetime
    attempts: int
    max_attempts: int
    queue: str
    required_capabilities: tuple[str, ...] = ()
    resource_requirements: dict[str, int] = Field(default_factory=dict)
    slot_cost: int = 1
    execution_timeout_seconds: int = 300
    batch_id: int | None = None
    parent_task_id: int | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    locked_by: str | None = None
    locked_at: datetime | None = None
    lease_token: str | None = None


class ResourceRecord(ContractModel):
    external_id: str = Field(min_length=1, max_length=1024)
    kind: str = Field(default="document", min_length=1, max_length=128)
    url: str | None = None
    title: str | None = None
    content: str | None = None
    content_type: str | None = None
    language: str | None = None
    published_at: datetime | None = None
    observed_at: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    tags: tuple[str, ...] = ()


class ResourceAsset(ContractModel):
    external_id: str
    asset_key: str
    url: str | None = None
    media_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    checksum: str | None = None
    storage_uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FetchReport(ContractModel):
    status: Literal["succeeded", "partial", "skipped"] = "succeeded"
    seen: int = 0
    added: int = 0
    updated: int = 0
    duplicate: int = 0
    assets: int = 0
    content_characters: int = 0
    bytes_downloaded: int = 0
    checkpoint: dict[str, Any] | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None


class PluginInventory(ContractModel):
    name: str
    kind: Literal["fetcher", "sink", "asset_store"]
    version: str
    api_version: str = "1"
    capabilities: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    default_queue: str | None = None

    @property
    def capability(self) -> str:
        return f"{self.kind}:{self.name}:api-v{self.api_version}"


class WorkerDescriptor(ContractModel):
    node_id: str
    instance_id: str
    hostname: str
    mode: Literal["resident", "burst"] = "resident"
    max_concurrency: int = Field(ge=1, le=128)
    software_version: str
    capabilities: tuple[str, ...] = ()
    queues: tuple[str, ...] = ("default",)
    plugins: tuple[PluginInventory, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
