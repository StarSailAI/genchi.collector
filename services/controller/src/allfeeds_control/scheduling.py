from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from allfeeds_contracts import ScheduleSpec
from croniter import croniter

UTC = UTC


def next_run(schedule: ScheduleSpec, after: datetime) -> datetime | None:
    after = after if after.tzinfo else after.replace(tzinfo=UTC)
    if schedule.type == "interval":
        return after + timedelta(seconds=int(schedule.seconds or 60))
    if schedule.type == "cron":
        zone = ZoneInfo(schedule.timezone)
        local = after.astimezone(zone)
        return croniter(str(schedule.cron), local).get_next(datetime).astimezone(UTC)
    if schedule.type == "once":
        at = schedule.at
        if at is None:
            return None
        at = at if at.tzinfo else at.replace(tzinfo=ZoneInfo(schedule.timezone))
        return at.astimezone(UTC) if at > after else None
    raise ValueError(f"unsupported schedule type: {schedule.type}")
