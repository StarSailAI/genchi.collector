from __future__ import annotations

from datetime import UTC, datetime, timedelta

from allfeeds_contracts import ScheduleSpec
from allfeeds_control.scheduling import next_run

UTC = UTC


def test_interval_next_run() -> None:
    now = datetime(2026, 7, 18, 0, 0, tzinfo=UTC)
    assert next_run(ScheduleSpec(type="interval", seconds=300), now) == now + timedelta(minutes=5)


def test_cron_respects_timezone() -> None:
    now = datetime(2026, 7, 18, 0, 30, tzinfo=UTC)
    result = next_run(
        ScheduleSpec(type="cron", cron="0 9 * * *", timezone="Asia/Shanghai"),
        now,
    )
    assert result == datetime(2026, 7, 18, 1, 0, tzinfo=UTC)
