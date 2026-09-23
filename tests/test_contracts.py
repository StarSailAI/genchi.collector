from __future__ import annotations

from datetime import UTC, datetime

import pytest
from allfeeds_contracts import ScheduleSpec, SourceSpec
from pydantic import ValidationError


def test_source_spec_builds_plugin_route() -> None:
    source = SourceSpec.model_validate(
        {
            "id": "news.example",
            "fetcher": "builtin.rss",
            "schedule": {"type": "interval", "seconds": 300},
            "config": {"url": "https://example.com/feed.xml"},
            "routing": {"queue": "web", "resources": {"domain:example.com": 2}},
        }
    )
    assert source.routing.queue == "web"
    assert source.routing.resources == {"domain:example.com": 2}
    assert source.retry.max_attempts == 3


def test_schedule_requires_variant_value() -> None:
    with pytest.raises(ValidationError):
        ScheduleSpec(type="cron")


def test_once_schedule_accepts_timestamp() -> None:
    value = ScheduleSpec(type="once", at=datetime(2026, 1, 1, tzinfo=UTC))
    assert value.type == "once"
