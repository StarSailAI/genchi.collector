from __future__ import annotations

from datetime import UTC, datetime

from allfeeds_contracts import FetchReport, ResourceRecord
from allfeeds_sdk import FetchContext, FetcherManifest, FetcherPlugin, FetchRequest
from pydantic import BaseModel, ConfigDict


class ExampleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message: str = "Hello from a custom plugin"


class ExampleFetcher(FetcherPlugin):
    manifest = FetcherManifest(
        name="example.hello",
        version="0.1.0",
        operations=("fetch", "backfill"),
    )
    config_model = ExampleConfig

    def fetch(self, context: FetchContext, request: FetchRequest) -> FetchReport:
        config = ExampleConfig.model_validate(request.config)
        now = datetime.now(UTC)
        context.emit(
            ResourceRecord(
                external_id=now.strftime("%Y-%m-%d"),
                kind="example",
                title="Example resource",
                content=config.message,
                observed_at=now,
                tags=request.tags,
            )
        )
        return FetchReport(details={"plugin": self.manifest.name})
