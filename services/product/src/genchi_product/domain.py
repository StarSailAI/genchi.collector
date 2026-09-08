from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

JST = ZoneInfo("Asia/Tokyo")
KINDS = {"LIVE", "FESTIVAL", "POPUP", "CAFE", "EXHIBITION", "MEETUP", "GOODS", "OTHER"}
MILESTONE_KINDS = {
    "ANNOUNCEMENT",
    "TICKET",
    "RESERVATION",
    "RESULT",
    "PAYMENT",
    "GOODS",
    "DOORS",
    "START",
    "PERIOD",
    "UPDATE",
}


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def canonical_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username:
            return None
        # Keep query parameters: several ticket providers put their native identity there.
        return urlunsplit(
            (parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, "")
        )
    except ValueError:
        return None


def classify(title: str, fallback: str = "OTHER") -> str:
    for pattern, kind in [
        (r"カフェ|cafe|café|喫茶", "CAFE"),
        (r"pop.?up|ポップアップ|快闪", "POPUP"),
        (r"展覧|展示|展$|原画展|exhibition", "EXHIBITION"),
        (r"物販|グッズ|通販|goods", "GOODS"),
        (r"お渡し|サイン会|握手|meet|ファンミ|トークイベント", "MEETUP"),
        (r"festival|フェス|fes\b", "FESTIVAL"),
        (r"live|ライブ|公演|コンサート", "LIVE"),
    ]:
        if re.search(pattern, title, re.I):
            return kind
    return {"FES": "FESTIVAL", "RELEASE_EVENT": "MEETUP"}.get(
        fallback, fallback if fallback in KINDS else "OTHER"
    )


def online_only(title: str) -> bool:
    return bool(
        re.search(r"ONLINE LIVE|生配信|ラジオ|RADIO|MV.*公開|放送|配信番組", title, re.I)
    ) and not bool(re.search(r"公開収録|現地|ライブビューイング|上映", title))


class Moment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    precision: Literal["TIME", "DATE", "TBD"] = "TBD"
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    timezone: str = "Asia/Tokyo"

    @model_validator(mode="after")
    def valid_time(self):
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValueError("Invalid timezone") from exc
        if self.precision == "TIME":
            if self.starts_at is None or self.starts_at.tzinfo is None:
                raise ValueError("Precise time requires an explicit timezone")
            if self.ends_at and (self.ends_at.tzinfo is None or self.ends_at < self.starts_at):
                raise ValueError("Window ends before it starts, or is missing its timezone")
            if self.starts_on or self.ends_on:
                raise ValueError("Date-only and precise time are mutually exclusive")
        elif self.precision == "DATE":
            if not self.starts_on or self.starts_at or self.ends_at:
                raise ValueError("Date-only time needs starts_on and must not invent a clock time")
            if self.ends_on and self.ends_on < self.starts_on:
                raise ValueError("Invalid date range")
        elif any((self.starts_at, self.ends_at, self.starts_on, self.ends_on)):
            raise ValueError("TBD must not include a fabricated date")
        return self

    def anchor(self) -> str:
        if self.starts_at:
            return self.starts_at.astimezone(JST).date().isoformat()
        return str(self.starts_on or "TBD")


def legacy_time(start: datetime | None, end: datetime | None = None) -> Moment:
    if start is None:
        return Moment()
    local = start.astimezone(JST)
    if local.hour == local.minute == local.second == 0:
        return Moment(
            precision="DATE",
            starts_on=local.date(),
            ends_on=end.astimezone(JST).date() if end and end >= start else None,
        )
    return Moment(
        precision="TIME",
        starts_at=start.astimezone(UTC),
        ends_at=end.astimezone(UTC) if end and end >= start else None,
    )


class EvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str | None = None
    external_id: str | None = None
    version_hash: str | None = None
    url: str | None = None
    excerpt: str = Field(min_length=1, max_length=4000)
    field_path: str = "activity"
    method: str = "structured"
    verified: bool = False
    published_at: datetime | None = None
    observed_at: datetime | None = None

    @field_validator("url")
    @classmethod
    def safe_url(cls, value):
        return canonical_url(value)


class MilestoneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_key: str
    kind: str
    title: str = Field(min_length=1, max_length=500)
    title_zh: str | None = Field(default=None, max_length=500)
    time: Moment = Field(default_factory=Moment)
    status: Literal["CONFIRMED", "REVIEW", "UNANNOUNCED", "CANCELED", "SUPERSEDED"] = "CONFIRMED"
    url: str | None = None
    platform: str | None = None
    round_key: str | None = None
    eligibility: str | None = None
    notes: str | None = None
    requires: Literal["NONE", "APPLIED", "WON"] = "NONE"
    details: dict = Field(default_factory=dict)
    evidence: EvidenceInput

    @model_validator(mode="after")
    def validate_kind(self):
        if self.kind not in MILESTONE_KINDS:
            raise ValueError("Unsupported milestone type")
        self.url = canonical_url(self.url)
        if self.time.precision == "TBD" and self.status == "CONFIRMED":
            self.status = "UNANNOUNCED"
        return self


class ActivityInput(BaseModel):
    activity_key: str | None = None
    model_config = ConfigDict(extra="forbid")
    source_key: str
    title: str = Field(min_length=1, max_length=1000)
    title_zh: str | None = Field(default=None, max_length=1000)
    kind: str = "OTHER"
    summary: str | None = None
    url: str | None = None
    subject_slugs: list[str] = Field(default_factory=list)
    time: Moment = Field(default_factory=Moment)
    occurrence_key: str | None = None
    venue: str | None = None
    city: str | None = None
    status: Literal["ANNOUNCED", "SCHEDULED", "POSTPONED", "CANCELED", "ENDED"] = "SCHEDULED"
    publication: Literal["PUBLISHED", "REVIEW", "REJECTED"] = "REVIEW"
    attendance: Literal["OFFLINE", "ONLINE", "HYBRID", "UNKNOWN"] = "OFFLINE"
    evidence: EvidenceInput
    milestones: list[MilestoneInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_activity(self):
        self.kind = classify(self.title, self.kind)
        self.url = canonical_url(self.url)
        if online_only(self.title):
            self.attendance = "ONLINE"
        return self
