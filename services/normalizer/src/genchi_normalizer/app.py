from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import psycopg
import requests
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .glossary import glossary_prompt

LOGGER = logging.getLogger("genchi.normalizer")
PROJECT_PREFIX = "project:"
COUNTRY_PREFIX = "country:"
TIMEZONE_PREFIX = "timezone:"
EVENT_TYPES = {"LIVE", "FES", "ANNIV", "RELEASE_EVENT", "RADIO", "OTHER"}
RELEASE_KINDS = {"CD", "BD", "DIGITAL", "GOODS"}
TICKET_PHASES = {
    "FC_PRE",
    "LOTTERY_1",
    "LOTTERY_2",
    "LOTTERY_3",
    "ADVANCE",
    "GENERAL",
    "DAY_OF",
    "RESALE",
    "OTHER",
}
TICKET_PLATFORMS = {"eplus", "lawson", "pia", "cnplayguide", "official", "other"}
ASOBI_SOURCE_TYPE = "asobi_ticket"
EPLUS_SOURCE_TYPE = "eplus_ticket"
TICKET_SOURCE_PLATFORMS = {
    EPLUS_SOURCE_TYPE: "eplus",
    "pia_ticket": "pia",
    "lawson_ticket": "lawson",
}
TICKET_UNKNOWN_PROJECTS = {"", "anime-general", "unknown"}
ACTIVITY_PROMPT_VERSION = "activity-v1"
ACTIVITY_AUTO_THRESHOLD = 0.95
JST = ZoneInfo("Asia/Tokyo")


@dataclass(frozen=True)
class Settings:
    database_url: str
    schema: str
    poll_seconds: float
    llm_base_url: str | None
    llm_api_key: str | None
    llm_model: str | None
    llm_response_format: str

    @classmethod
    def from_env(cls) -> Settings:
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        schema = os.environ.get("GENCHI_DB_SCHEMA", "genchi")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise RuntimeError("GENCHI_DB_SCHEMA is invalid")
        llm_response_format = (
            os.environ.get("LLM_RESPONSE_FORMAT", "auto").strip().lower() or "auto"
        )
        if llm_response_format not in {"auto", "json_schema", "json_object"}:
            raise RuntimeError("LLM_RESPONSE_FORMAT must be auto, json_schema, or json_object")
        return cls(
            database_url=database_url,
            schema=schema,
            poll_seconds=max(0.2, float(os.environ.get("NORMALIZER_POLL_SECONDS", "2"))),
            llm_base_url=os.environ.get("LLM_BASE_URL", "").strip() or None,
            llm_api_key=os.environ.get("LLM_API_KEY", "").strip() or None,
            llm_model=os.environ.get("LLM_MODEL", "").strip() or None,
            llm_response_format=llm_response_format,
        )

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)


def _stable_id(namespace: str, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"genchi:{namespace}:{value}"))


def _tag(tags: list[str], prefix: str, default: str | None = None) -> str | None:
    return next((value.removeprefix(prefix) for value in tags if value.startswith(prefix)), default)


def _category(text: str) -> str:
    lowered = text.lower()
    if any(value in lowered for value in ("チケット", "先行", "抽選", "一般発売", "受付")):
        return "EVENT"
    if any(value in lowered for value in ("live", "ライブ", "公演", "festival", "フェス")):
        return "EVENT"
    if any(
        value in lowered
        for value in ("release", "リリース", "発売", "配信", "album", "single", "blu-ray")
    ):
        return "RELEASE"
    if any(value in lowered for value in ("動画", "放送", "radio", "ラジオ", "配信番組")):
        return "MEDIA"
    return "OTHER"


def _ticket_payload(resource: dict[str, Any]) -> dict[str, Any]:
    attributes = resource.get("attributes") or {}
    payload = attributes.get("ticket_page") or attributes.get("eplus_ticket") or {}
    return payload if isinstance(payload, dict) else {}


def _ticket_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        unescape(str(item)).strip()
        for item in value
        if item is not None and unescape(str(item)).strip()
    ]


def _ticket_relevance_rules(
    resource: dict[str, Any],
    *,
    platform: str,
) -> dict[str, Any]:
    payload = _ticket_payload(resource)
    if payload.get("discoveryScope") == "jpop":
        return {
            "status": "review",
            "confidence": 0.5,
            "method": "music-pilot",
            "reason": "J-pop 试点来源需核实艺人身份、实体场次和票务轮次",
            "signals": ["discovery-scope:jpop"],
            "subjectName": None,
            "subjectType": "UNKNOWN",
        }
    discoveries = [item for item in payload.get("discovery") or [] if isinstance(item, dict)]
    native_categories = _ticket_string_list(
        payload.get("nativeCategories") or payload.get("relatedGenres")
    )
    if not native_categories:
        match = re.search(r"(?:^|\n)ジャンル:\s*([^\n]+)", resource.get("content") or "")
        if match:
            native_categories = [match.group(1).strip()]
    search_queries = _ticket_string_list(
        [item.get("searchQuery") for item in discoveries if item.get("searchQuery")]
    )
    if not search_queries:
        query = parse_qs(urlparse(str(resource.get("url") or "")).query)
        search_queries = _ticket_string_list(query.get("keyword") or query.get("kw"))

    signals: list[str] = []
    trusted_sources = [
        str(item.get("sourceUrl") or "") for item in discoveries if item.get("trustedCategory")
    ]
    if trusted_sources:
        signals.extend(f"trusted-category:{value}" for value in trusted_sources)
        return {
            "status": "accepted",
            "confidence": 0.99,
            "method": "rule",
            "reason": "由票务平台的动画专属分类页发现",
            "signals": signals,
            "subjectName": None,
            "subjectType": "UNKNOWN",
        }

    signals.extend(f"native-category:{value}" for value in native_categories)
    signals.extend(f"search-query:{value}" for value in search_queries)
    signals.extend(
        f"configured-discovery:{value}"
        for value in _ticket_string_list(payload.get("matchedKeywords"))
    )
    return {
        "status": "review",
        "confidence": 0.5,
        "method": "structured-gate",
        "reason": f"{platform} 的结构化发现信息只能用于召回，需要语义判断",
        "signals": signals,
        "subjectName": None,
        "subjectType": "UNKNOWN",
    }


def _activity_identity_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", unescape(value)).casefold()
    return re.sub(r"[\W_]+", "", normalized)


def _activity_fingerprint(value: str) -> str:
    identity = _activity_identity_text(value)
    return hashlib.sha256((identity or value.strip()).encode()).hexdigest()


def _activity_titles(resource: dict[str, Any]) -> list[str]:
    payload = _ticket_payload(resource)
    titles = [
        unescape(str(item.get("name") or "")).strip()
        for item in payload.get("events") or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    asobi_act = ((resource.get("attributes") or {}).get("asobi_ticket") or {}).get("act") or {}
    asobi_title = str((asobi_act.get("attributes") or {}).get("name") or "").strip()
    if asobi_title:
        titles.append(unescape(asobi_title))
    if not titles and str(resource.get("title") or "").strip():
        titles = [unescape(str(resource["title"])).strip()]
    unique: dict[str, str] = {}
    for title in titles:
        unique.setdefault(_activity_fingerprint(title), title)
    return list(unique.values())


def _activity_validation_conflicts(resource: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    payload = _ticket_payload(resource)
    source_times = {
        parsed.astimezone(UTC).replace(second=0, microsecond=0)
        for item in payload.get("events") or []
        if isinstance(item, dict)
        for parsed in [_parse_time(item.get("startsAt"))]
        if parsed
    }
    conflicts: list[str] = []
    llm_times = [
        parsed.astimezone(UTC).replace(second=0, microsecond=0)
        for value in decision.get("sessionTimes") or []
        for parsed in [_parse_time(value)]
        if parsed
    ]
    if source_times and llm_times and not any(value in source_times for value in llm_times):
        conflicts.append("LLM sessionTimes do not match structured platform times")
    if decision.get("status") == "accepted" and not decision.get("shortTitle"):
        conflicts.append("accepted activity is missing shortTitle")
    if decision.get("status") == "accepted" and not decision.get("evidence"):
        conflicts.append("accepted activity is missing evidence")
    return conflicts


def _news_kind(category: str) -> str:
    return {"EVENT": "EVENT", "RELEASE": "RELEASE", "MEDIA": "MEDIA"}.get(category, "OTHER")


def _slug(title: str | None, suffix: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (title or "news").lower()).strip("-")[:48] or "news"
    return f"{base}-{suffix[:10]}"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=UTC)
    except ValueError:
        return None


def _event_match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", unescape(value)).casefold()
    normalized = re.sub(
        r"^(?:一般発売|先行|抽選|プレリザーブ|プレリク)\s*[＜<【\[].*?[＞>】\]]\s*[／/]\s*",
        "",
        normalized,
    )
    return re.sub(r"[\W_]+", "", normalized)


def _same_event_title(left: str, right: str) -> bool:
    left_key = _event_match_text(left)
    right_key = _event_match_text(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    shorter, longer = sorted((left_key, right_key), key=len)
    return len(shorter) >= 8 and shorter in longer and len(shorter) / len(longer) >= 0.72


def _asobi_real_acts(resource: dict[str, Any]) -> list[dict[str, Any]]:
    payload = (resource.get("attributes") or {}).get("asobi_ticket") or {}
    related_acts = [item for item in payload.get("acts") or [] if isinstance(item, dict)]
    included_acts = [
        item
        for item in payload.get("included") or []
        if isinstance(item, dict) and item.get("type") == "act"
    ]

    def real_acts(acts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            act
            for act in acts
            if "通し券" not in str((act.get("attributes") or {}).get("name") or "")
            and "通しチケット" not in str((act.get("attributes") or {}).get("name") or "")
        ]

    # A resale reception carries its exact act in `acts`. Prefer that relationship
    # over the booth-wide JSON:API `included` collection. Multi-day pass pseudo acts
    # are not curated Events, so those deliberately fall back to the real booth acts
    # and are narrowed from the dates and venue in the reception name below.
    return real_acts(related_acts) or real_acts(included_acts)


def _asobi_reception_dates(value: str) -> set[tuple[int, int]]:
    dates: set[tuple[int, int]] = set()
    current_month: int | None = None
    for match in re.finditer(r"(?:(?P<month>\d{1,2})月)?(?P<day>\d{1,2})日(?!間)", value):
        if match.group("month"):
            current_month = int(match.group("month"))
        if current_month is None:
            continue
        day = int(match.group("day"))
        if 1 <= current_month <= 12 and 1 <= day <= 31:
            dates.add((current_month, day))
    for match in re.finditer(r"(?<!\d)(?P<month>\d{1,2})/(?P<day>\d{1,2})(?!\d)", value):
        month = int(match.group("month"))
        day = int(match.group("day"))
        if 1 <= month <= 12 and 1 <= day <= 31:
            dates.add((month, day))
    return dates


def _asobi_act_date(act: dict[str, Any]) -> tuple[int, int] | None:
    raw_date = str((act.get("attributes") or {}).get("performance_date") or "")
    try:
        performance_date = datetime.fromisoformat(raw_date).date()
    except ValueError:
        return None
    return performance_date.month, performance_date.day


def _asobi_location_score(
    reception_name: str, act: dict[str, Any], location_tokens: tuple[str, ...]
) -> int:
    lowered = reception_name.lower()
    attributes = act.get("attributes") or {}
    location_text = " ".join(
        str(value or "") for value in (attributes.get("name"), attributes.get("venue"))
    ).lower()
    return sum(1 for token in location_tokens if token in lowered and token in location_text)


def _asobi_match_acts(reception_name: str, acts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(acts) <= 1:
        return acts
    location_tokens = (
        "namba",
        "osaka",
        "nagoya",
        "haneda",
        "tokyo",
        "yokohama",
        "fukuoka",
        "大阪",
        "愛知",
        "東京",
        "神奈川",
        "福岡",
        "北海道",
        "宮城",
        "千葉",
        "埼玉",
        "兵庫",
        "広島",
    )
    requested_dates = _asobi_reception_dates(reception_name)
    if requested_dates:
        date_matches = [act for act in acts if _asobi_act_date(act) in requested_dates]
        if date_matches:
            date_and_location_matches = [
                act
                for act in date_matches
                if _asobi_location_score(reception_name, act, location_tokens) > 0
            ]
            return date_and_location_matches or date_matches

    location_matches = [
        act for act in acts if _asobi_location_score(reception_name, act, location_tokens) > 0
    ]
    return location_matches or acts


def _asobi_ticket_phase(entry_type: str, name: str) -> str:
    if entry_type == "resale_lottery":
        return "RESALE"
    if entry_type == "fcfs":
        return "GENERAL"
    if "プレミアム" in name:
        return "FC_PRE"
    if "3次" in name:
        return "LOTTERY_3"
    if "2次" in name:
        return "LOTTERY_2"
    if entry_type == "lottery":
        return "LOTTERY_1"
    return "OTHER"


def _asobi_ticket_status(value: str | None) -> str | None:
    return {
        "before_entry_period": "UPCOMING",
        "within_entry_period": "OPEN",
        "after_entry_period": "CLOSED",
    }.get(value or "")


def _eplus_event_type(title: str) -> str:
    lowered = title.lower()
    if "フェス" in lowered or "festival" in lowered:
        return "FES"
    if any(
        value in lowered
        for value in (
            "展",
            "展覧会",
            "原画展",
            "企画展",
            "ミュージアム",
            "博物館",
            "美術館",
            "上映",
            "舞台",
            "ミュージカル",
            "朗読劇",
            "トークショー",
            "ファンミーティング",
            " cafe",
            "カフェ",
        )
    ):
        return "OTHER"
    return "LIVE"


class Normalizer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def connect(self):
        return psycopg.connect(
            self.settings.database_url,
            row_factory=dict_row,
            options=f"-c search_path={self.settings.schema},allfeeds,public",
        )

    def claim(self) -> dict[str, Any] | None:
        with self.connect() as conn, conn.transaction():
            conn.execute(
                """
                UPDATE "NormalizationJob" SET "status"='RETRY',"lockedAt"=NULL,
                    "notBefore"=NOW(),"updatedAt"=NOW(),"lastError"='stale processing lease'
                WHERE "status"='PROCESSING' AND "lockedAt"<NOW()-INTERVAL '15 minutes'
                """
            )
            return conn.execute(
                """
                WITH candidate AS (
                    SELECT "id" FROM "NormalizationJob"
                    WHERE "status" IN ('PENDING','RETRY') AND "notBefore"<=NOW()
                    ORDER BY "notBefore","id" FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE "NormalizationJob" job SET "status"='PROCESSING',"lockedAt"=NOW(),
                    "attempts"=job."attempts"+1,"updatedAt"=NOW()
                FROM candidate WHERE job."id"=candidate."id" RETURNING job.*
                """
            ).fetchone()

    def finish(self, job_id: int) -> None:
        with self.connect() as conn, conn.transaction():
            conn.execute(
                """
                UPDATE "NormalizationJob" SET "status"='DONE',"lockedAt"=NULL,
                    "finishedAt"=NOW(),"updatedAt"=NOW(),"lastError"=NULL WHERE "id"=%s
                """,
                (job_id,),
            )

    def fail(self, job: dict[str, Any], exc: Exception) -> None:
        attempts = int(job["attempts"])
        terminal = attempts >= 8
        delay = min(3600, 30 * 2 ** max(0, attempts - 1))
        with self.connect() as conn, conn.transaction():
            conn.execute(
                """
                UPDATE "NormalizationJob" SET "status"=%s,"lockedAt"=NULL,
                    "notBefore"=NOW()+(%s*INTERVAL '1 second'),"lastError"=%s,
                    "updatedAt"=NOW(),"finishedAt"=CASE WHEN %s THEN NOW() ELSE NULL END
                WHERE "id"=%s
                """,
                (
                    "DEAD" if terminal else "RETRY",
                    delay,
                    f"{type(exc).__name__}: {exc}"[:4000],
                    terminal,
                    job["id"],
                ),
            )

    def resource(self, resource_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT r.*,s.spec AS source_spec FROM allfeeds.resources r
                LEFT JOIN allfeeds.sources s ON s.source_id=r.source_id WHERE r.id=%s
                """,
                (resource_id,),
            ).fetchone()
        if not row:
            raise RuntimeError(f"raw resource {resource_id} no longer exists")
        return row

    def _call_llm_json(
        self,
        *,
        schema_name: str,
        schema: dict[str, Any],
        prompt: str,
        system_prompt: str,
        max_tokens: int,
    ) -> dict[str, Any] | None:
        if not self.settings.llm_enabled:
            return None
        endpoint = self.settings.llm_base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions" if endpoint.endswith("/v1") else "/v1/chat/completions"
        response_format = self.settings.llm_response_format
        if response_format == "auto":
            hostname = (urlparse(endpoint).hostname or "").lower()
            model = (self.settings.llm_model or "").lower()
            response_format = (
                "json_object"
                if hostname == "deepseek.com"
                or hostname.endswith(".deepseek.com")
                or model.startswith("deepseek")
                else "json_schema"
            )
        format_payload = (
            {"type": "json_object"}
            if response_format == "json_object"
            else {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            }
        )
        response = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
            json={
                "model": self.settings.llm_model,
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt + glossary_prompt(),
                    },
                    {"role": "user", "content": prompt},
                ],
                "response_format": format_payload,
                "temperature": 0,
                "max_tokens": max_tokens,
            },
            timeout=90,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text[:1000]
            if self.settings.llm_api_key:
                detail = detail.replace(self.settings.llm_api_key, "[REDACTED]")
            raise RuntimeError(f"LLM HTTP {response.status_code}: {detail}") from exc
        payload = response.json()
        choice = payload["choices"][0]
        content = choice["message"]["content"]
        if not content:
            raise RuntimeError("LLM returned empty content")
        if choice.get("finish_reason") == "length":
            raise RuntimeError("LLM JSON output was truncated")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise RuntimeError("LLM output is not a JSON object")
        return result

    def call_llm(self, resource: dict[str, Any], category: str) -> dict[str, Any] | None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["titleZh", "summaryZh", "category", "facts"],
            "properties": {
                "titleZh": {"type": ["string", "null"]},
                "summaryZh": {"type": ["string", "null"]},
                "category": {"type": "string", "enum": ["EVENT", "RELEASE", "MEDIA", "OTHER"]},
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "confidence", "data"],
                        "properties": {
                            "kind": {"type": "string", "enum": ["EVENT", "TICKET", "RELEASE"]},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "data": {"type": "object"},
                        },
                    },
                },
            },
        }
        prompt = (
            "Return one JSON object and no Markdown. Extract only explicit facts from this Japanese official "
            "announcement. Keep uncertain dates or event references out of facts. Translate only the title and "
            "a short summary. All timestamps must be ISO 8601 with an explicit UTC offset.\n"
            "The JSON shape is: "
            '{"titleZh":string|null,"summaryZh":string|null,'
            '"category":"EVENT|RELEASE|MEDIA|OTHER","facts":['
            '{"kind":"EVENT|TICKET|RELEASE","confidence":number,"data":object}]}.\n'
            "EVENT data fields: titleJa, titleZh, startsAt, endsAt, doorsAt, eventType "
            "(LIVE|FES|ANNIV|RELEASE_EVENT|RADIO|OTHER), officialUrl.\n"
            "TICKET data fields: eventOfficialUrl, phase "
            "(FC_PRE|LOTTERY_1|LOTTERY_2|LOTTERY_3|ADVANCE|GENERAL|DAY_OF|RESALE|OTHER), "
            "phaseLabelJa, opensAt, closesAt, resultAt, platform "
            "(eplus|lawson|pia|cnplayguide|official|other), url. If EVENT and TICKET describe the same event, "
            "their officialUrl and eventOfficialUrl must be exactly identical.\n"
            "RELEASE data fields: titleJa, titleZh, releaseOn, kind (CD|BD|DIGITAL|GOODS), officialUrl.\n"
            f"Rule category: {category}\nURL: {resource.get('url')}\n"
            f"Title: {resource.get('title')}\nBody:\n{(resource.get('content') or '')[:16000]}"
        )
        enrichment = self._call_llm_json(
            schema_name="genchi_content",
            schema=schema,
            prompt=prompt,
            system_prompt="You normalize official Japanese music and live-event information.",
            max_tokens=4096,
        )
        if enrichment is None:
            return None
        if not isinstance(enrichment, dict) or not isinstance(enrichment.get("facts"), list):
            raise RuntimeError("LLM output does not match the expected object shape")
        return enrichment

    def call_ticket_relevance_llm(
        self,
        resource: dict[str, Any],
        *,
        platform: str,
        rule_decision: dict[str, Any],
    ) -> dict[str, Any] | None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "isRelevant",
                "confidence",
                "reason",
                "subjectName",
                "subjectType",
                "canonicalTitle",
                "shortTitle",
                "projectName",
                "eventType",
                "works",
                "performers",
                "venues",
                "sessionTimes",
                "ticketPhases",
                "evidence",
            ],
            "properties": {
                "isRelevant": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
                "subjectName": {"type": ["string", "null"]},
                "subjectType": {
                    "type": "string",
                    "enum": [
                        "IP",
                        "ARTIST",
                        "VOICE_ACTOR",
                        "VTUBER",
                        "EVENT_SERIES",
                        "UNKNOWN",
                    ],
                },
                "canonicalTitle": {"type": ["string", "null"]},
                "shortTitle": {"type": ["string", "null"]},
                "projectName": {"type": ["string", "null"]},
                "eventType": {
                    "type": ["string", "null"],
                    "enum": [
                        "LIVE",
                        "FES",
                        "ANNIV",
                        "RELEASE_EVENT",
                        "RADIO",
                        "OTHER",
                        None,
                    ],
                },
                "works": {"type": "array", "items": {"type": "string"}},
                "performers": {"type": "array", "items": {"type": "string"}},
                "venues": {"type": "array", "items": {"type": "string"}},
                "sessionTimes": {"type": "array", "items": {"type": "string"}},
                "ticketPhases": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": sorted(TICKET_PHASES),
                    },
                },
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
        }
        payload = _ticket_payload(resource)
        prompt = (
            "Return one JSON object and no Markdown. Decide whether this Japanese ticket listing belongs in "
            "an anime-culture calendar. Include events directly connected to anime, manga, games, light novels, "
            "voice actors, anisong, VTubers, 2.5D adaptations, or creators performing specifically in those "
            "roles. Exclude ordinary theatre, unrelated J-pop/rock bands, sports, tourism admission, and generic "
            "events. A search query such as アニメ, 声優, or ゲーム is discovery context only and is not evidence "
            "by itself. Base the decision on the actual title, platform categories, and listing text. "
            "Extract other explicit facts in the same call so they can be cross-checked against platform data. "
            "subjectName is the recognizable work, franchise, performer, group, or event-series. "
            "canonicalTitle is the official activity title without ticketing wrappers. shortTitle is a concise, "
            "recognizable Japanese or commonly-used proper name for a calendar; preserve names and never truncate "
            "them mechanically. projectName is the franchise or project when explicit. sessionTimes must be ISO "
            "8601 timestamps with offsets. evidence contains short exact fragments from the listing supporting "
            "the relevance decision and extracted identity. Use null or an empty list instead of guessing.\n"
            "JSON shape: "
            '{"isRelevant":boolean,"confidence":number,"reason":string,'
            '"subjectName":string|null,'
            '"subjectType":"IP|ARTIST|VOICE_ACTOR|VTUBER|EVENT_SERIES|UNKNOWN",'
            '"canonicalTitle":string|null,"shortTitle":string|null,"projectName":string|null,'
            '"eventType":"LIVE|FES|ANNIV|RELEASE_EVENT|RADIO|OTHER"|null,'
            '"works":string[],"performers":string[],"venues":string[],'
            '"sessionTimes":string[],"ticketPhases":string[],"evidence":string[]}.\n'
            f"Platform: {platform}\n"
            f"URL: {resource.get('url')}\n"
            f"Title: {resource.get('title')}\n"
            f"Native categories: {json.dumps(payload.get('nativeCategories') or payload.get('relatedGenres') or [], ensure_ascii=False)}\n"
            f"Discovery: {json.dumps(payload.get('discovery') or [], ensure_ascii=False)}\n"
            f"Rule signals: {json.dumps(rule_decision.get('signals') or [], ensure_ascii=False)}\n"
            f"Listing:\n{(resource.get('content') or '')[:12000]}"
        )
        result = self._call_llm_json(
            schema_name="genchi_ticket_relevance",
            schema=schema,
            prompt=prompt,
            system_prompt="You are a conservative Japanese anime-culture ticket relevance classifier.",
            max_tokens=1800,
        )
        if result is None:
            return None
        required = {
            "isRelevant",
            "confidence",
            "reason",
            "subjectName",
            "subjectType",
            "canonicalTitle",
            "shortTitle",
            "projectName",
            "eventType",
            "works",
            "performers",
            "venues",
            "sessionTimes",
            "ticketPhases",
            "evidence",
        }
        if not required.issubset(result):
            raise RuntimeError("LLM ticket relevance output is missing required fields")
        confidence = float(result["confidence"])
        if not 0 <= confidence <= 1 or not isinstance(result["isRelevant"], bool):
            raise RuntimeError("LLM ticket relevance output has invalid values")
        subject_type = str(result["subjectType"])
        if subject_type not in {
            "IP",
            "ARTIST",
            "VOICE_ACTOR",
            "VTUBER",
            "EVENT_SERIES",
            "UNKNOWN",
        }:
            raise RuntimeError("LLM ticket relevance output has invalid subjectType")
        status = (
            "accepted"
            if result["isRelevant"] and confidence >= ACTIVITY_AUTO_THRESHOLD
            else "rejected"
            if not result["isRelevant"] and confidence >= ACTIVITY_AUTO_THRESHOLD
            else "review"
        )
        decision = {
            "status": status,
            "confidence": confidence,
            "method": "llm",
            "reason": str(result["reason"]).strip(),
            "signals": list(rule_decision.get("signals") or []),
            "subjectName": (
                str(result["subjectName"]).strip() if result.get("subjectName") else None
            ),
            "subjectType": subject_type,
            "canonicalTitle": (
                str(result["canonicalTitle"]).strip() if result.get("canonicalTitle") else None
            ),
            "shortTitle": (str(result["shortTitle"]).strip() if result.get("shortTitle") else None),
            "projectName": (
                str(result["projectName"]).strip() if result.get("projectName") else None
            ),
            "eventType": (
                str(result["eventType"]) if result.get("eventType") in EVENT_TYPES else None
            ),
            "works": [
                str(value).strip() for value in result.get("works") or [] if str(value).strip()
            ],
            "performers": [
                str(value).strip() for value in result.get("performers") or [] if str(value).strip()
            ],
            "venues": [
                str(value).strip() for value in result.get("venues") or [] if str(value).strip()
            ],
            "sessionTimes": [
                str(value).strip()
                for value in result.get("sessionTimes") or []
                if str(value).strip()
            ],
            "ticketPhases": [
                str(value)
                for value in result.get("ticketPhases") or []
                if str(value) in TICKET_PHASES
            ],
            "evidence": [
                str(value).strip() for value in result.get("evidence") or [] if str(value).strip()
            ],
        }
        conflicts = _activity_validation_conflicts(resource, decision)
        decision["validationConflicts"] = conflicts
        if conflicts:
            decision["status"] = "review"
        return decision

    def cached_activity_decision(self, resource: dict[str, Any]) -> dict[str, Any] | None:
        titles = _activity_titles(resource)
        if not titles:
            return None
        fingerprints = [_activity_fingerprint(title) for title in titles]
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM "ActivityProfile"
                WHERE "fingerprint" = ANY(%s) AND "promptVersion"=%s
                ORDER BY "updatedAt" DESC
                """,
                (fingerprints, ACTIVITY_PROMPT_VERSION),
            ).fetchall()
        if not rows:
            return None
        row = rows[0]
        if row["reviewStatus"] == "REVIEW" and row.get("contentHash") != resource.get(
            "content_hash"
        ):
            return None
        facts = dict(row.get("facts") or {})
        facts.update(
            {
                "status": str(row["reviewStatus"]).lower(),
                "confidence": float(row["relevanceConfidence"]),
                "method": "activity-cache",
                "canonicalTitle": row.get("canonicalTitle"),
                "shortTitle": row.get("shortTitle"),
                "subjectName": row.get("subjectName"),
                "subjectType": row.get("subjectType") or "UNKNOWN",
                "eventType": row.get("eventType"),
                "evidence": list(row.get("evidence") or []),
            }
        )
        return facts

    def store_activity_profiles(
        self,
        resource: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, str]:
        titles = _activity_titles(resource)
        if not titles:
            return {}
        tags = [str(value) for value in resource.get("tags") or []]
        project = _tag(tags, PROJECT_PREFIX)
        status = str(decision.get("status") or "review").upper()
        if status not in {"ACCEPTED", "REVIEW", "REJECTED"}:
            status = "REVIEW"
        confidence = float(decision.get("confidence") or 0)
        mapping: dict[str, str] = {}
        with self.connect() as conn, conn.transaction():
            ip = (
                conn.execute('SELECT "id" FROM "Ip" WHERE "slug"=%s', (project,)).fetchone()
                if project and project not in TICKET_UNKNOWN_PROJECTS
                else None
            )
            for title in titles:
                fingerprint = _activity_fingerprint(title)
                profile_id = _stable_id("activity", fingerprint)
                short_title = str(decision.get("shortTitle") or "").strip() or None
                canonical_title = str(decision.get("canonicalTitle") or "").strip() or title
                slug = _slug(short_title or canonical_title, fingerprint)
                conn.execute(
                    """
                    INSERT INTO "ActivityProfile" (
                        "id","slug","fingerprint","canonicalTitle","shortTitle",
                        "subjectName","subjectType","ipId","eventType",
                        "relevanceConfidence","reviewStatus","facts","evidence",
                        "contentHash","llmModel","promptVersion"
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT ("fingerprint") DO UPDATE SET
                        "canonicalTitle"=EXCLUDED."canonicalTitle",
                        "shortTitle"=COALESCE(EXCLUDED."shortTitle","ActivityProfile"."shortTitle"),
                        "subjectName"=COALESCE(EXCLUDED."subjectName","ActivityProfile"."subjectName"),
                        "subjectType"=EXCLUDED."subjectType",
                        "ipId"=COALESCE(EXCLUDED."ipId","ActivityProfile"."ipId"),
                        "eventType"=COALESCE(EXCLUDED."eventType","ActivityProfile"."eventType"),
                        "relevanceConfidence"=EXCLUDED."relevanceConfidence",
                        "reviewStatus"=EXCLUDED."reviewStatus",
                        "facts"=EXCLUDED."facts","evidence"=EXCLUDED."evidence",
                        "contentHash"=EXCLUDED."contentHash","llmModel"=EXCLUDED."llmModel",
                        "promptVersion"=EXCLUDED."promptVersion","updatedAt"=NOW()
                    """,
                    (
                        profile_id,
                        slug,
                        fingerprint,
                        canonical_title,
                        short_title,
                        decision.get("subjectName"),
                        decision.get("subjectType") or "UNKNOWN",
                        ip["id"] if ip else None,
                        decision.get("eventType"),
                        confidence,
                        status,
                        Jsonb(decision),
                        Jsonb(decision.get("evidence") or []),
                        resource.get("content_hash"),
                        self.settings.llm_model,
                        ACTIVITY_PROMPT_VERSION,
                    ),
                )
                mapping[fingerprint] = profile_id
        return mapping

    def backfill_activity_profiles(self, *, limit: int | None = None) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT e."id",e."titleJa",e."titleZh",e."startsAt",e."doorsAt",
                    e."eventType",e."officialUrl",e."activityId",
                    i."slug" AS "projectSlug",
                    COALESCE(v."nameJa",v."nameZh") AS "venueName",
                    COALESCE(
                        jsonb_agg(
                            jsonb_build_object(
                                'phase',t."phase",'label',t."phaseLabelJa",
                                'opensAt',t."opensAt",'closesAt',t."closesAt",
                                'resultAt',t."resultAt",'platform',t."platform"
                            )
                        ) FILTER (WHERE t."id" IS NOT NULL),
                        '[]'::jsonb
                    ) AS tickets
                FROM "Event" e
                LEFT JOIN "Ip" i ON i."id"=e."ipId"
                LEFT JOIN "Venue" v ON v."id"=e."venueId"
                LEFT JOIN "TicketWindow" t ON t."eventId"=e."id"
                GROUP BY e."id",i."slug",v."nameJa",v."nameZh"
                ORDER BY e."startsAt",e."id"
                """
            ).fetchall()
        families: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            title = str(row.get("titleJa") or row.get("titleZh") or "").strip()
            if not title:
                continue
            families.setdefault(_activity_fingerprint(title), []).append(row)
        selected = list(families.items())
        if limit is not None:
            selected = selected[: max(0, limit)]
        counts = {
            "families": 0,
            "accepted": 0,
            "review": 0,
            "rejected": 0,
            "linked": 0,
            "errors": 0,
        }
        for fingerprint, family in selected:
            representative = family[0]
            title = str(
                representative.get("titleJa") or representative.get("titleZh") or ""
            ).strip()
            project = str(representative.get("projectSlug") or "unknown")
            event_items = [
                {
                    "id": str(item["id"]),
                    "name": str(item.get("titleJa") or item.get("titleZh") or title),
                    "startsAt": item["startsAt"].isoformat(),
                    "doorsAt": item["doorsAt"].isoformat() if item.get("doorsAt") else None,
                    "venue": {"name": item.get("venueName")},
                    "ticketWindows": list(item.get("tickets") or []),
                }
                for item in family
            ]
            assessment_items = (
                event_items if len(event_items) <= 9 else [*event_items[:8], event_items[-1]]
            )
            content = "\n".join(
                [
                    f"Official title: {title}",
                    f"Configured project: {project}",
                    f"Total sessions: {len(event_items)}",
                    "Sessions:",
                    *[
                        f"- {item['startsAt']} / {(item.get('venue') or {}).get('name') or 'unknown venue'}"
                        for item in assessment_items
                    ],
                    "Ticket phases:",
                    *[
                        f"- {ticket.get('phase')} {ticket.get('label') or ''}"
                        for item in assessment_items
                        for ticket in item.get("ticketWindows") or []
                    ],
                ]
            )
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            resource = {
                "title": title,
                "content": content,
                "url": representative.get("officialUrl"),
                "content_hash": content_hash,
                "attributes": {
                    "source_type": "activity_backfill",
                    "ticket_page": {
                        "pageId": f"backfill:{fingerprint}",
                        "events": assessment_items,
                        "nativeCategories": [],
                        "discovery": [],
                    },
                },
                "tags": [f"project:{project}"],
            }
            decision = self.cached_activity_decision(resource)
            if not decision:
                try:
                    decision = self.call_ticket_relevance_llm(
                        resource,
                        platform="curated",
                        rule_decision={
                            "status": "review",
                            "confidence": 0.5,
                            "method": "structured-gate",
                            "reason": "legacy activity family requires semantic assessment",
                            "signals": ["curated-event-family"],
                            "subjectName": None,
                            "subjectType": "UNKNOWN",
                        },
                    )
                except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                    counts["errors"] += 1
                    LOGGER.warning(
                        "activity backfill LLM output rejected fingerprint=%s title=%r error=%s",
                        fingerprint,
                        title,
                        exc,
                    )
            if not decision:
                decision = {
                    "status": "review",
                    "confidence": 0,
                    "method": "llm-unavailable",
                    "reason": "activity backfill requires LLM",
                    "signals": [],
                    "subjectName": None,
                    "subjectType": "UNKNOWN",
                }
            mapping = self.store_activity_profiles(resource, decision)
            activity_id = mapping.get(fingerprint)
            if activity_id:
                event_ids = [str(item["id"]) for item in family]
                with self.connect() as conn, conn.transaction():
                    conn.execute(
                        'UPDATE "Event" SET "activityId"=%s,"updatedAt"=NOW() WHERE "id" = ANY(%s)',
                        (activity_id, event_ids),
                    )
                counts["linked"] += len(event_ids)
            status = str(decision.get("status") or "review")
            counts[status if status in {"accepted", "review", "rejected"} else "review"] += 1
            counts["families"] += 1
        return counts

    def upsert_content(
        self,
        resource: dict[str, Any],
        *,
        enrichment: dict[str, Any] | None,
        category: str,
    ) -> tuple[str, str]:
        tags = [str(value) for value in resource.get("tags") or []]
        project = _tag(tags, PROJECT_PREFIX)
        country = _tag(tags, COUNTRY_PREFIX, "JP") or "JP"
        timezone = _tag(tags, TIMEZONE_PREFIX, "Asia/Tokyo") or "Asia/Tokyo"
        source_type = (resource.get("attributes") or {}).get("source_type")
        if source_type in TICKET_SOURCE_PLATFORMS and project in TICKET_UNKNOWN_PROJECTS:
            project = "unknown"
        source_kind = (
            "TWITTER"
            if resource["kind"] == "x_post"
            else "AGGREGATOR"
            if source_type in TICKET_SOURCE_PLATFORMS
            else "OFFICIAL"
        )
        source_id = _stable_id("source", resource["source_id"])
        content_id = _stable_id("content", f"{resource['source_id']}:{resource['external_id']}")
        title_zh = enrichment.get("titleZh") if enrichment else None
        summary_zh = enrichment.get("summaryZh") if enrichment else None
        final_category = str(enrichment.get("category") or category) if enrichment else category
        attributes = dict(resource.get("attributes") or {})
        media = attributes.get("media") or []
        published_at = (
            resource.get("published_at") or resource.get("observed_at") or datetime.now(UTC)
        )
        suffix = hashlib.sha256(content_id.encode()).hexdigest()
        news_id = _stable_id("news", content_id)
        news_slug = _slug(resource.get("title"), suffix)
        cover_url = next(
            (
                item.get("url")
                for item in media
                if isinstance(item, dict) and item.get("type") == "image"
            ),
            None,
        )
        search_text = "\n".join(
            value
            for value in (
                resource.get("title"),
                title_zh,
                resource.get("content"),
                summary_zh,
                project,
            )
            if value
        )
        with self.connect() as conn, conn.transaction():
            conn.execute(
                """
                INSERT INTO "Source" ("id","key","name","url","kind","projectKey","country","timezone")
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT ("key") DO UPDATE SET "url"=EXCLUDED."url","kind"=EXCLUDED."kind",
                    "projectKey"=EXCLUDED."projectKey","country"=EXCLUDED."country",
                    "timezone"=EXCLUDED."timezone","isActive"=TRUE
                """,
                (
                    source_id,
                    resource["source_id"],
                    resource["source_id"],
                    resource.get("url"),
                    source_kind,
                    project,
                    country,
                    timezone,
                ),
            )
            conn.execute(
                """
                INSERT INTO "ContentItem" (
                    "id","sourceId","externalId","rawResourceId","rawContentHash","kind",
                    "canonicalUrl","titleOriginal","bodyOriginal","language","titleZh","summaryZh",
                    "category","projectKey","country","timezone","publishedAt","observedAt",
                    "media","metadata","processingStatus"
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT ("sourceId","externalId") DO UPDATE SET
                    "rawResourceId"=EXCLUDED."rawResourceId","rawContentHash"=EXCLUDED."rawContentHash",
                    "kind"=EXCLUDED."kind","canonicalUrl"=EXCLUDED."canonicalUrl",
                    "titleOriginal"=EXCLUDED."titleOriginal","bodyOriginal"=EXCLUDED."bodyOriginal",
                    "language"=EXCLUDED."language","titleZh"=COALESCE(EXCLUDED."titleZh","ContentItem"."titleZh"),
                    "summaryZh"=COALESCE(EXCLUDED."summaryZh","ContentItem"."summaryZh"),
                    "category"=EXCLUDED."category","projectKey"=EXCLUDED."projectKey",
                    "publishedAt"=EXCLUDED."publishedAt","observedAt"=EXCLUDED."observedAt",
                    "media"=EXCLUDED."media","metadata"=EXCLUDED."metadata",
                    "processingStatus"=EXCLUDED."processingStatus","updatedAt"=NOW()
                """,
                (
                    content_id,
                    source_id,
                    resource["external_id"],
                    resource["id"],
                    resource["content_hash"],
                    resource["kind"],
                    resource.get("url"),
                    resource.get("title"),
                    resource.get("content"),
                    resource.get("language"),
                    title_zh,
                    summary_zh,
                    final_category,
                    project,
                    country,
                    timezone,
                    published_at,
                    resource.get("observed_at") or datetime.now(UTC),
                    Jsonb(media),
                    Jsonb({"attributes": attributes, "tags": tags}),
                    "ENRICHED" if enrichment else "READY",
                ),
            )
            conn.execute(
                """
                INSERT INTO "NewsPost" (
                    "id","slug","contentItemId","titleZh","titleJa","summary","contentMd",
                    "publishedAt","kind","sourceId","coverUrl"
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT ("contentItemId") DO UPDATE SET
                    "titleZh"=EXCLUDED."titleZh","titleJa"=EXCLUDED."titleJa",
                    "summary"=EXCLUDED."summary","contentMd"=EXCLUDED."contentMd",
                    "publishedAt"=EXCLUDED."publishedAt","kind"=EXCLUDED."kind",
                    "sourceId"=EXCLUDED."sourceId","coverUrl"=EXCLUDED."coverUrl"
                """,
                (
                    news_id,
                    news_slug,
                    content_id,
                    title_zh,
                    resource.get("title"),
                    summary_zh,
                    resource.get("content"),
                    published_at,
                    _news_kind(final_category),
                    source_id,
                    cover_url,
                ),
            )
            search_id = _stable_id("search", f"CONTENT:{content_id}")
            conn.execute(
                """
                INSERT INTO "SearchDocument" (
                    "id","entityType","entityId","titleOriginal","titleZh","bodyOriginal",
                    "summaryZh","projectKey","kind","country","publishedAt","canonicalUrl","searchText"
                ) VALUES (%s,'CONTENT',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT ("entityType","entityId") DO UPDATE SET
                    "titleOriginal"=EXCLUDED."titleOriginal","titleZh"=EXCLUDED."titleZh",
                    "bodyOriginal"=EXCLUDED."bodyOriginal","summaryZh"=EXCLUDED."summaryZh",
                    "projectKey"=EXCLUDED."projectKey","kind"=EXCLUDED."kind",
                    "country"=EXCLUDED."country","publishedAt"=EXCLUDED."publishedAt",
                    "canonicalUrl"=EXCLUDED."canonicalUrl","searchText"=EXCLUDED."searchText","updatedAt"=NOW()
                """,
                (
                    search_id,
                    content_id,
                    resource.get("title"),
                    title_zh,
                    resource.get("content"),
                    summary_zh,
                    project,
                    resource["kind"],
                    country,
                    published_at,
                    resource.get("url"),
                    search_text,
                ),
            )
            if project:
                ip = conn.execute('SELECT "id" FROM "Ip" WHERE "slug"=%s', (project,)).fetchone()
                if ip:
                    conn.execute(
                        'INSERT INTO "ContentIp" ("contentItemId","ipId") VALUES (%s,%s) ON CONFLICT DO NOTHING',
                        (content_id, ip["id"]),
                    )
                    conn.execute(
                        'INSERT INTO "NewsIp" ("newsId","ipId") VALUES (%s,%s) ON CONFLICT DO NOTHING',
                        (news_id, ip["id"]),
                    )
        return content_id, source_id

    def candidate(self, content_id: str, fact: dict[str, Any], *, automatic: bool = True) -> None:
        kind = str(fact.get("kind") or "")
        payload = fact.get("data") if isinstance(fact.get("data"), dict) else {}
        confidence = float(fact.get("confidence") or 0)
        digest = hashlib.sha256(
            json.dumps(
                {"kind": kind, "payload": payload}, ensure_ascii=False, sort_keys=True
            ).encode()
        ).hexdigest()
        candidate_id = _stable_id("candidate", f"{content_id}:{digest}")
        with self.connect() as conn, conn.transaction():
            conn.execute(
                """
                INSERT INTO "ExtractionCandidate" ("id","contentItemId","kind","payload","confidence")
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT ("id") DO UPDATE SET
                    "payload"=EXCLUDED."payload","confidence"=EXCLUDED."confidence","updatedAt"=NOW()
                """,
                (candidate_id, content_id, kind, Jsonb(payload), confidence),
            )
        if automatic:
            self.apply_candidate(candidate_id, force=False, reviewer=None)

    def upsert_asobi_event(
        self,
        resource: dict[str, Any],
        *,
        source_id: str,
        activity_profiles: dict[str, str] | None = None,
    ) -> str | None:
        payload = (resource.get("attributes") or {}).get("asobi_ticket") or {}
        act = payload.get("act") or {}
        act_id = str(act.get("id") or "")
        attributes = act.get("attributes") or {}
        title = str(attributes.get("name") or resource.get("title") or "")
        starts_at = _parse_time(attributes.get("performance_starts_at"))
        if not act_id or not title or not starts_at or "通し券" in title or "通しチケット" in title:
            return None
        source_key = f"asobi:act:{act_id}"
        entity_id = _stable_id("event", source_key)
        activity_id = (activity_profiles or {}).get(_activity_fingerprint(title))
        official_url = f"{resource.get('url')}#act-{act_id}"
        project = _tag([str(value) for value in resource.get("tags") or []], PROJECT_PREFIX)
        media = (resource.get("attributes") or {}).get("media") or []
        key_visual_url = next(
            (
                item.get("url")
                for item in media
                if isinstance(item, dict) and item.get("type") == "image" and item.get("url")
            ),
            None,
        )
        with self.connect() as conn, conn.transaction():
            ip = conn.execute('SELECT "id" FROM "Ip" WHERE "slug"=%s', (project,)).fetchone()
            ip_id = ip["id"] if ip else None
            slug = _slug(title, hashlib.sha256(source_key.encode()).hexdigest())
            conn.execute(
                """
                INSERT INTO "Event" (
                    "id","slug","sourceKey","titleJa","startsAt","endsAt","doorsAt",
                    "ipId","activityId","eventType","officialUrl","keyVisualUrl","sourceId"
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'LIVE',%s,%s,%s)
                ON CONFLICT ("sourceKey") DO UPDATE SET
                    "titleJa"=EXCLUDED."titleJa","startsAt"=EXCLUDED."startsAt",
                    "endsAt"=EXCLUDED."endsAt","doorsAt"=EXCLUDED."doorsAt",
                    "ipId"=EXCLUDED."ipId","officialUrl"=EXCLUDED."officialUrl",
                    "activityId"=COALESCE(EXCLUDED."activityId","Event"."activityId"),
                    "keyVisualUrl"=EXCLUDED."keyVisualUrl","sourceId"=EXCLUDED."sourceId",
                    "updatedAt"=NOW()
                """,
                (
                    entity_id,
                    slug,
                    source_key,
                    title,
                    starts_at,
                    _parse_time(attributes.get("performance_ends_at")),
                    _parse_time(attributes.get("opens_at")),
                    ip_id,
                    activity_id,
                    official_url,
                    key_visual_url,
                    source_id,
                ),
            )
            search_text = "\n".join(value for value in (title, project) if value)
            conn.execute(
                """
                INSERT INTO "SearchDocument" (
                    "id","entityType","entityId","titleOriginal","projectKey","kind",
                    "country","publishedAt","canonicalUrl","searchText"
                ) VALUES (%s,'EVENT',%s,%s,%s,'EVENT','JP',%s,%s,%s)
                ON CONFLICT ("entityType","entityId") DO UPDATE SET
                    "titleOriginal"=EXCLUDED."titleOriginal","projectKey"=EXCLUDED."projectKey",
                    "publishedAt"=EXCLUDED."publishedAt","canonicalUrl"=EXCLUDED."canonicalUrl",
                    "searchText"=EXCLUDED."searchText","updatedAt"=NOW()
                """,
                (
                    _stable_id("search", f"EVENT:{entity_id}"),
                    entity_id,
                    title,
                    project,
                    starts_at,
                    official_url,
                    search_text,
                ),
            )
        return entity_id

    def upsert_asobi_tickets(self, resource: dict[str, Any]) -> int:
        payload = (resource.get("attributes") or {}).get("asobi_ticket") or {}
        reception = payload.get("reception") or {}
        reception_id = str(reception.get("id") or "")
        attributes = reception.get("attributes") or {}
        name = str(attributes.get("name") or resource.get("title") or "")
        opens_at = _parse_time(attributes.get("entry_period_starts_at"))
        if not reception_id or not name or not opens_at:
            return 0
        acts = _asobi_match_acts(name, _asobi_real_acts(resource))
        if not acts:
            return 0
        phase = _asobi_ticket_phase(str(attributes.get("entry_type") or ""), name)
        status = _asobi_ticket_status(attributes.get("entry_period_status"))
        closes_at = _parse_time(attributes.get("entry_period_ends_at"))
        result_at = _parse_time(attributes.get("result_announcement_scheduled_at"))
        applied = 0
        source_keys: list[str] = []
        with self.connect() as conn, conn.transaction():
            for act in acts:
                act_id = str(act.get("id") or "")
                if not act_id:
                    continue
                event_source_key = f"asobi:act:{act_id}"
                event = conn.execute(
                    'SELECT "id" FROM "Event" WHERE "sourceKey"=%s', (event_source_key,)
                ).fetchone()
                if not event:
                    continue
                source_key = f"asobi:reception:{reception_id}:act:{act_id}"
                source_keys.append(source_key)
                entity_id = _stable_id("ticket", source_key)
                conn.execute(
                    """
                    INSERT INTO "TicketWindow" (
                        "id","sourceKey","eventId","phase","phaseLabelJa","opensAt",
                        "closesAt","resultAt","platform","url","status"
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'official',%s,%s)
                    ON CONFLICT ("sourceKey") DO UPDATE SET
                        "eventId"=EXCLUDED."eventId","phase"=EXCLUDED."phase",
                        "phaseLabelJa"=EXCLUDED."phaseLabelJa","opensAt"=EXCLUDED."opensAt",
                        "closesAt"=EXCLUDED."closesAt","resultAt"=EXCLUDED."resultAt",
                        "platform"=EXCLUDED."platform","url"=EXCLUDED."url",
                        "status"=EXCLUDED."status"
                    """,
                    (
                        entity_id,
                        source_key,
                        event["id"],
                        phase,
                        name,
                        opens_at,
                        closes_at,
                        result_at,
                        resource.get("url"),
                        status,
                    ),
                )
                applied += 1
            if source_keys:
                source_prefix = f"asobi:reception:{reception_id}:act:"
                conn.execute(
                    """
                    DELETE FROM "TicketWindow"
                    WHERE LEFT("sourceKey", %s)=%s
                      AND NOT ("sourceKey" = ANY(%s))
                    """,
                    (len(source_prefix), source_prefix, source_keys),
                )
        return applied

    @staticmethod
    def _matching_ticket_event(
        conn: psycopg.Connection,
        *,
        event_source_key: str,
        title: str,
        starts_at: datetime,
        venue_name: str,
    ) -> tuple[str, bool]:
        exact = conn.execute(
            'SELECT "id" FROM "Event" WHERE "sourceKey"=%s',
            (event_source_key,),
        ).fetchone()
        if exact:
            return exact["id"], False
        local_day = starts_at.astimezone(JST).date()
        lower = datetime.combine(local_day, datetime.min.time(), tzinfo=JST)
        upper = lower + timedelta(days=1)
        rows = conn.execute(
            """
            SELECT e."id",e."titleJa",v."nameJa" AS "venueName"
            FROM "Event" e
            LEFT JOIN "Venue" v ON v."id"=e."venueId"
            WHERE e."startsAt">=%s AND e."startsAt"<%s
            """,
            (lower, upper),
        ).fetchall()
        venue_key = _event_match_text(venue_name)
        matches = [
            row
            for row in rows
            if _same_event_title(title, str(row.get("titleJa") or ""))
            and (not venue_key or venue_key == _event_match_text(str(row.get("venueName") or "")))
        ]
        if len(matches) == 1:
            return matches[0]["id"], True
        return _stable_id("event", event_source_key), False

    def upsert_ticket_page(
        self,
        resource: dict[str, Any],
        *,
        source_id: str,
        platform: str,
        activity_profiles: dict[str, str] | None = None,
    ) -> tuple[int, int]:
        attributes = resource.get("attributes") or {}
        payload = attributes.get("ticket_page") or attributes.get("eplus_ticket") or {}
        if platform not in TICKET_PLATFORMS:
            return 0, 0
        page_id = str(payload.get("pageId") or "")
        events = [item for item in payload.get("events") or [] if isinstance(item, dict)]
        if not page_id or not events:
            return 0, 0
        project = str(
            payload.get("project")
            or _tag(
                [str(value) for value in resource.get("tags") or []],
                PROJECT_PREFIX,
                "unknown",
            )
            or "unknown"
        )
        if project in TICKET_UNKNOWN_PROJECTS:
            project = "unknown"
        media = attributes.get("media") or []
        key_visual_url = next(
            (
                item.get("url")
                for item in media
                if isinstance(item, dict) and item.get("type") == "image" and item.get("url")
            ),
            None,
        )
        event_count = 0
        ticket_count = 0
        ticket_source_keys: list[str] = []
        ticket_source_prefix = f"{platform}:reception:{page_id}:"
        with self.connect() as conn, conn.transaction():
            ip = (
                conn.execute('SELECT "id" FROM "Ip" WHERE "slug"=%s', (project,)).fetchone()
                if project != "unknown"
                else None
            )
            ip_id = ip["id"] if ip else None
            for item in events:
                performance_id = str(item.get("id") or "")
                title = unescape(str(item.get("name") or resource.get("title") or "")).strip()
                starts_at = _parse_time(item.get("startsAt"))
                if not performance_id or not title or not starts_at:
                    continue
                event_source_key = f"{platform}:event:{performance_id}"
                event_url = str(item.get("url") or resource.get("url") or "")
                venue = item.get("venue") if isinstance(item.get("venue"), dict) else {}
                venue_name = unescape(str(venue.get("name") or "")).strip()
                venue_url = str(venue.get("url") or "").strip() or None
                activity_id = (activity_profiles or {}).get(_activity_fingerprint(title))
                event_id, reused_event = self._matching_ticket_event(
                    conn,
                    event_source_key=event_source_key,
                    title=title,
                    starts_at=starts_at,
                    venue_name=venue_name,
                )
                venue_id = None
                if venue_name:
                    venue_match = re.search(r"/sf/venue/(\d+)", venue_url or "")
                    venue_key = (
                        f"{platform}:{venue_match.group(1)}"
                        if venue_match
                        else f"{platform}:{venue_name}:{venue.get('prefecture') or ''}"
                    )
                    venue_id = _stable_id("venue", venue_key)
                    venue_slug_suffix = (
                        venue_match.group(1)
                        if venue_match
                        else hashlib.sha256(venue_key.encode()).hexdigest()[:12]
                    )
                    conn.execute(
                        """
                        INSERT INTO "Venue" (
                            "id","slug","nameJa","prefecture","officialUrl"
                        ) VALUES (%s,%s,%s,%s,%s)
                        ON CONFLICT ("id") DO UPDATE SET
                            "nameJa"=EXCLUDED."nameJa",
                            "prefecture"=EXCLUDED."prefecture",
                            "officialUrl"=EXCLUDED."officialUrl"
                        """,
                        (
                            venue_id,
                            f"{platform}-venue-{venue_slug_suffix}",
                            venue_name,
                            venue.get("prefecture"),
                            venue_url,
                        ),
                    )
                if reused_event:
                    conn.execute(
                        """
                        UPDATE "Event"
                        SET "ipId"=COALESCE("ipId",%s),
                            "activityId"=COALESCE("activityId",%s),"updatedAt"=NOW()
                        WHERE "id"=%s
                        """,
                        (ip_id, activity_id, event_id),
                    )
                else:
                    slug = _slug(
                        title,
                        hashlib.sha256(event_source_key.encode()).hexdigest(),
                    )
                    conn.execute(
                        """
                        INSERT INTO "Event" (
                            "id","slug","sourceKey","titleJa","startsAt","endsAt","doorsAt",
                            "venueId","ipId","activityId","eventType","officialUrl","keyVisualUrl","sourceId"
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT ("sourceKey") DO UPDATE SET
                            "titleJa"=EXCLUDED."titleJa","startsAt"=EXCLUDED."startsAt",
                            "endsAt"=EXCLUDED."endsAt","doorsAt"=EXCLUDED."doorsAt",
                            "venueId"=EXCLUDED."venueId","ipId"=EXCLUDED."ipId",
                            "activityId"=COALESCE(EXCLUDED."activityId","Event"."activityId"),
                            "eventType"=EXCLUDED."eventType","officialUrl"=EXCLUDED."officialUrl",
                            "keyVisualUrl"=EXCLUDED."keyVisualUrl","sourceId"=EXCLUDED."sourceId",
                            "updatedAt"=NOW()
                        """,
                        (
                            event_id,
                            slug,
                            event_source_key,
                            title,
                            starts_at,
                            _parse_time(item.get("endsAt")),
                            _parse_time(item.get("doorsAt")),
                            venue_id,
                            ip_id,
                            activity_id,
                            _eplus_event_type(title),
                            event_url,
                            key_visual_url,
                            source_id,
                        ),
                    )
                    search_text = "\n".join(
                        value for value in (title, venue_name, project) if value
                    )
                    conn.execute(
                        """
                        INSERT INTO "SearchDocument" (
                            "id","entityType","entityId","titleOriginal","projectKey","kind",
                            "country","publishedAt","canonicalUrl","searchText"
                        ) VALUES (%s,'EVENT',%s,%s,%s,'EVENT','JP',%s,%s,%s)
                        ON CONFLICT ("entityType","entityId") DO UPDATE SET
                            "titleOriginal"=EXCLUDED."titleOriginal",
                            "projectKey"=EXCLUDED."projectKey",
                            "publishedAt"=EXCLUDED."publishedAt",
                            "canonicalUrl"=EXCLUDED."canonicalUrl",
                            "searchText"=EXCLUDED."searchText","updatedAt"=NOW()
                        """,
                        (
                            _stable_id("search", f"EVENT:{event_id}"),
                            event_id,
                            title,
                            project,
                            starts_at,
                            event_url,
                            search_text,
                        ),
                    )
                event_count += 1
                for window in item.get("ticketWindows") or []:
                    if not isinstance(window, dict):
                        continue
                    window_id = str(window.get("id") or "")
                    opens_at = _parse_time(window.get("opensAt"))
                    if not window_id or not opens_at:
                        continue
                    # Multiple upstream performance rows can normalize to the same
                    # Event (for example, duplicated timed-entry rows on Pia).
                    # Key the association by the resolved Event so one reception
                    # produces only one TicketWindow for that canonical Event.
                    source_key = f"{ticket_source_prefix}{window_id}:event:{event_id}"
                    ticket_source_keys.append(source_key)
                    ticket_id = _stable_id("ticket", source_key)
                    phase = window.get("phase") if window.get("phase") in TICKET_PHASES else "OTHER"
                    status = (
                        window.get("status")
                        if window.get("status")
                        in {"UPCOMING", "OPEN", "CLOSED", "RESULT_ANNOUNCED", "CANCELED"}
                        else None
                    )
                    conn.execute(
                        """
                        INSERT INTO "TicketWindow" (
                            "id","sourceKey","eventId","phase","phaseLabelJa","opensAt",
                            "closesAt","resultAt","platform","url","status"
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT ("sourceKey") DO UPDATE SET
                            "eventId"=EXCLUDED."eventId","phase"=EXCLUDED."phase",
                            "phaseLabelJa"=EXCLUDED."phaseLabelJa",
                            "opensAt"=EXCLUDED."opensAt","closesAt"=EXCLUDED."closesAt",
                            "resultAt"=EXCLUDED."resultAt",
                            "platform"=EXCLUDED."platform","url"=EXCLUDED."url",
                            "status"=EXCLUDED."status"
                        """,
                        (
                            ticket_id,
                            source_key,
                            event_id,
                            phase,
                            window.get("label"),
                            opens_at,
                            _parse_time(window.get("closesAt")),
                            _parse_time(window.get("resultAt")),
                            platform,
                            str(window.get("url") or event_url),
                            status,
                        ),
                    )
                    ticket_count += 1
            if ticket_source_keys:
                conn.execute(
                    """
                    DELETE FROM "TicketWindow"
                    WHERE LEFT("sourceKey", %s)=%s
                      AND NOT ("sourceKey" = ANY(%s))
                    """,
                    (
                        len(ticket_source_prefix),
                        ticket_source_prefix,
                        ticket_source_keys,
                    ),
                )
            else:
                conn.execute(
                    'DELETE FROM "TicketWindow" WHERE LEFT("sourceKey", %s)=%s',
                    (len(ticket_source_prefix), ticket_source_prefix),
                )
        return event_count, ticket_count

    def set_ticket_relevance(
        self,
        content_id: str,
        decision: dict[str, Any],
    ) -> None:
        processing_status = {
            "accepted": "READY",
            "review": "REVIEW",
            "rejected": "REJECTED",
        }.get(str(decision.get("status")), "REVIEW")
        with self.connect() as conn, conn.transaction():
            conn.execute(
                """
                UPDATE "ContentItem"
                SET "metadata"=jsonb_set(
                        COALESCE("metadata",'{}'::jsonb),
                        '{ticketRelevance}',
                        %s,
                        TRUE
                    ),
                    "processingStatus"=%s,
                    "updatedAt"=NOW()
                WHERE "id"=%s
                """,
                (Jsonb(decision), processing_status, content_id),
            )

    def remove_ticket_page_projection(
        self,
        resource: dict[str, Any],
        *,
        platform: str,
    ) -> tuple[int, int]:
        payload = _ticket_payload(resource)
        page_id = str(payload.get("pageId") or "")
        if not page_id:
            return 0, 0
        event_source_keys = [
            f"{platform}:event:{item.get('id')}"
            for item in payload.get("events") or []
            if isinstance(item, dict) and item.get("id")
        ]
        ticket_prefix = f"{platform}:reception:{page_id}:"
        with self.connect() as conn, conn.transaction():
            deleted_tickets = conn.execute(
                """
                DELETE FROM "TicketWindow"
                WHERE LEFT("sourceKey", %s)=%s
                RETURNING "eventId"
                """,
                (len(ticket_prefix), ticket_prefix),
            ).fetchall()
            candidate_event_ids = {
                str(row["eventId"]) for row in deleted_tickets if row.get("eventId")
            }
            if event_source_keys:
                candidate_event_ids.update(
                    str(row["id"])
                    for row in conn.execute(
                        'SELECT "id" FROM "Event" WHERE "sourceKey" = ANY(%s)',
                        (event_source_keys,),
                    ).fetchall()
                )
            removable_event_ids: list[str] = []
            if candidate_event_ids:
                removable_event_ids = [
                    str(row["id"])
                    for row in conn.execute(
                        """
                        SELECT e."id"
                        FROM "Event" e
                        WHERE e."id" = ANY(%s)
                          AND e."sourceKey" = ANY(%s)
                          AND NOT EXISTS (
                              SELECT 1 FROM "TicketWindow" t WHERE t."eventId"=e."id"
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM "EventNews" n WHERE n."eventId"=e."id"
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM "EventArtist" a WHERE a."eventId"=e."id"
                          )
                        """,
                        (list(candidate_event_ids), event_source_keys or [""]),
                    ).fetchall()
                ]
            if removable_event_ids:
                conn.execute(
                    """
                    DELETE FROM "SearchDocument"
                    WHERE "entityType"='EVENT' AND "entityId" = ANY(%s)
                    """,
                    (removable_event_ids,),
                )
                conn.execute(
                    'DELETE FROM "Event" WHERE "id" = ANY(%s)',
                    (removable_event_ids,),
                )
        return len(deleted_tickets), len(removable_event_ids)

    def remove_news_projection(self, content_id: str) -> None:
        with self.connect() as conn, conn.transaction():
            conn.execute('DELETE FROM "NewsPost" WHERE "contentItemId"=%s', (content_id,))

    def apply_candidate(
        self, candidate_id: str, *, force: bool, reviewer: str | None
    ) -> tuple[str | None, list[str]]:
        with self.connect() as conn:
            candidate = conn.execute(
                """
                SELECT c.*,i."sourceId",i."projectKey",i."canonicalUrl"
                FROM "ExtractionCandidate" c JOIN "ContentItem" i ON i."id"=c."contentItemId"
                WHERE c."id"=%s
                """,
                (candidate_id,),
            ).fetchone()
        if not candidate:
            raise KeyError(candidate_id)
        kind = candidate["kind"]
        payload = dict(candidate["payload"] or {})
        threshold = 0.95 if kind == "TICKET" else 0.90
        if not force and float(candidate["confidence"]) < threshold:
            return None, ["confidence below automatic threshold"]
        errors: list[str] = []
        entity_id: str | None = None
        entity_time: datetime | None = None
        with self.connect() as conn, conn.transaction():
            ip = conn.execute(
                'SELECT "id" FROM "Ip" WHERE "slug"=%s', (candidate["projectKey"],)
            ).fetchone()
            ip_id = ip["id"] if ip else None
            if kind == "EVENT":
                starts_at = _parse_time(payload.get("startsAt"))
                if not payload.get("titleJa"):
                    errors.append("titleJa is required")
                if not starts_at:
                    errors.append("valid startsAt is required")
                if not errors:
                    entity_time = starts_at
                    entity_id = _stable_id("event", candidate_id)
                    event_type = (
                        payload.get("eventType")
                        if payload.get("eventType") in EVENT_TYPES
                        else "LIVE"
                    )
                    slug = _slug(
                        payload.get("titleJa"), hashlib.sha256(candidate_id.encode()).hexdigest()
                    )
                    conn.execute(
                        """
                        INSERT INTO "Event" (
                            "id","slug","sourceKey","titleJa","titleZh","startsAt","endsAt","doorsAt",
                            "ipId","eventType","officialUrl","sourceId"
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT ("sourceKey") DO UPDATE SET
                            "titleJa"=EXCLUDED."titleJa","titleZh"=EXCLUDED."titleZh",
                            "startsAt"=EXCLUDED."startsAt","endsAt"=EXCLUDED."endsAt",
                            "doorsAt"=EXCLUDED."doorsAt","eventType"=EXCLUDED."eventType",
                            "officialUrl"=EXCLUDED."officialUrl","updatedAt"=NOW()
                        """,
                        (
                            entity_id,
                            slug,
                            candidate_id,
                            payload.get("titleJa"),
                            payload.get("titleZh"),
                            starts_at,
                            _parse_time(payload.get("endsAt")),
                            _parse_time(payload.get("doorsAt")),
                            ip_id,
                            event_type,
                            payload.get("officialUrl") or candidate["canonicalUrl"],
                            candidate["sourceId"],
                        ),
                    )
            elif kind == "RELEASE":
                release_on = _parse_time(payload.get("releaseOn"))
                if not payload.get("titleJa"):
                    errors.append("titleJa is required")
                if not release_on:
                    errors.append("valid releaseOn is required")
                if not errors:
                    entity_time = release_on
                    entity_id = _stable_id("release", candidate_id)
                    release_kind = (
                        payload.get("kind") if payload.get("kind") in RELEASE_KINDS else "CD"
                    )
                    slug = _slug(
                        payload.get("titleJa"), hashlib.sha256(candidate_id.encode()).hexdigest()
                    )
                    conn.execute(
                        """
                        INSERT INTO "ReleaseItem" (
                            "id","slug","sourceKey","titleJa","titleZh","kind","releaseOn","ipId","officialUrl"
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT ("sourceKey") DO UPDATE SET
                            "titleJa"=EXCLUDED."titleJa","titleZh"=EXCLUDED."titleZh",
                            "kind"=EXCLUDED."kind","releaseOn"=EXCLUDED."releaseOn",
                            "officialUrl"=EXCLUDED."officialUrl"
                        """,
                        (
                            entity_id,
                            slug,
                            candidate_id,
                            payload.get("titleJa"),
                            payload.get("titleZh"),
                            release_kind,
                            release_on,
                            ip_id,
                            payload.get("officialUrl") or candidate["canonicalUrl"],
                        ),
                    )
            elif kind == "TICKET":
                opens_at = _parse_time(payload.get("opensAt"))
                event_url = payload.get("eventOfficialUrl")
                event = (
                    conn.execute(
                        'SELECT "id" FROM "Event" WHERE "officialUrl"=%s ORDER BY "updatedAt" DESC LIMIT 1',
                        (event_url,),
                    ).fetchone()
                    if event_url
                    else None
                )
                closes_at = _parse_time(payload.get("closesAt"))
                if not event:
                    errors.append("exact eventOfficialUrl match is required")
                if not opens_at:
                    errors.append("valid opensAt is required")
                if opens_at and closes_at and closes_at < opens_at:
                    errors.append("closesAt is before opensAt")
                if not errors:
                    entity_id = _stable_id("ticket", candidate_id)
                    phase = (
                        payload.get("phase") if payload.get("phase") in TICKET_PHASES else "OTHER"
                    )
                    platform = (
                        payload.get("platform")
                        if payload.get("platform") in TICKET_PLATFORMS
                        else "official"
                    )
                    conn.execute(
                        """
                        INSERT INTO "TicketWindow" (
                            "id","sourceKey","eventId","phase","phaseLabelJa","opensAt","closesAt",
                            "resultAt","platform","url"
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT ("sourceKey") DO UPDATE SET
                            "eventId"=EXCLUDED."eventId","phase"=EXCLUDED."phase",
                            "phaseLabelJa"=EXCLUDED."phaseLabelJa","opensAt"=EXCLUDED."opensAt",
                            "closesAt"=EXCLUDED."closesAt","resultAt"=EXCLUDED."resultAt",
                            "platform"=EXCLUDED."platform","url"=EXCLUDED."url"
                        """,
                        (
                            entity_id,
                            candidate_id,
                            event["id"],
                            phase,
                            payload.get("phaseLabelJa"),
                            opens_at,
                            closes_at,
                            _parse_time(payload.get("resultAt")),
                            platform,
                            payload.get("url"),
                        ),
                    )
            else:
                errors.append("unknown candidate kind")
            if entity_id and kind in {"EVENT", "RELEASE"}:
                title_original = payload.get("titleJa")
                title_zh = payload.get("titleZh")
                canonical_url = payload.get("officialUrl") or candidate["canonicalUrl"]
                search_text = "\n".join(
                    value for value in (title_original, title_zh, candidate["projectKey"]) if value
                )
                conn.execute(
                    """
                    INSERT INTO "SearchDocument" (
                        "id","entityType","entityId","titleOriginal","titleZh","projectKey",
                        "kind","country","publishedAt","canonicalUrl","searchText"
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,'JP',%s,%s,%s)
                    ON CONFLICT ("entityType","entityId") DO UPDATE SET
                        "titleOriginal"=EXCLUDED."titleOriginal","titleZh"=EXCLUDED."titleZh",
                        "projectKey"=EXCLUDED."projectKey","publishedAt"=EXCLUDED."publishedAt",
                        "canonicalUrl"=EXCLUDED."canonicalUrl","searchText"=EXCLUDED."searchText",
                        "updatedAt"=NOW()
                    """,
                    (
                        _stable_id("search", f"{kind}:{entity_id}"),
                        kind,
                        entity_id,
                        title_original,
                        title_zh,
                        candidate["projectKey"],
                        kind,
                        entity_time,
                        canonical_url,
                        search_text,
                    ),
                )
            status = (
                "APPROVED"
                if reviewer and not errors
                else "AUTO_APPLIED"
                if not errors
                else "PENDING"
            )
            conn.execute(
                """
                UPDATE "ExtractionCandidate" SET "status"=%s,"validationErrors"=%s,
                    "appliedEntityType"=%s,"appliedEntityId"=%s,"reviewedBy"=%s,
                    "reviewedAt"=CASE WHEN CAST(%s AS TEXT) IS NULL THEN "reviewedAt" ELSE NOW() END,
                    "updatedAt"=NOW()
                WHERE "id"=%s
                """,
                (
                    status,
                    Jsonb(errors),
                    kind if entity_id else None,
                    entity_id,
                    reviewer,
                    reviewer,
                    candidate_id,
                ),
            )
        return entity_id, errors

    def process(self, job: dict[str, Any]) -> None:
        resource = self.resource(job["resourceId"])
        if resource["content_hash"] != job["contentHash"]:
            self.finish(job["id"])
            return
        category = _category(f"{resource.get('title') or ''}\n{resource.get('content') or ''}")
        content_id, _ = self.upsert_content(resource, enrichment=None, category=category)
        source_type = (resource.get("attributes") or {}).get("source_type")
        if source_type == ASOBI_SOURCE_TYPE:
            source_id = _stable_id("source", resource["source_id"])
            if resource["kind"] == "ticket_act":
                rule_decision = {
                    "status": "review",
                    "confidence": 0.5,
                    "method": "structured-gate",
                    "reason": "official ticket act requires one semantic activity assessment",
                    "signals": ["official-ticket-act"],
                    "subjectName": None,
                    "subjectType": "UNKNOWN",
                }
                decision = self.cached_activity_decision(resource)
                if not decision:
                    decision = self.call_ticket_relevance_llm(
                        resource,
                        platform="official",
                        rule_decision=rule_decision,
                    )
                if not decision:
                    decision = rule_decision
                activity_profiles = self.store_activity_profiles(resource, decision)
                self.upsert_asobi_event(
                    resource,
                    source_id=source_id,
                    activity_profiles=activity_profiles,
                )
            elif resource["kind"] == "ticket_reception":
                applied = self.upsert_asobi_tickets(resource)
                reception = ((resource.get("attributes") or {}).get("asobi_ticket") or {}).get(
                    "reception"
                ) or {}
                entry_status = (reception.get("attributes") or {}).get("entry_period_status")
                if applied == 0 and entry_status == "within_entry_period":
                    enrichment = self.call_llm(resource, category)
                    if enrichment:
                        content_id, _ = self.upsert_content(
                            resource, enrichment=enrichment, category=category
                        )
                        for fact in enrichment.get("facts") or []:
                            if isinstance(fact, dict) and fact.get("kind") in {
                                "EVENT",
                                "TICKET",
                                "RELEASE",
                            }:
                                self.candidate(content_id, fact)
            elif resource["kind"] == "ticket_booth":
                enrichment = self.call_llm(resource, category)
                if enrichment:
                    self.upsert_content(resource, enrichment=enrichment, category=category)
            self.finish(job["id"])
            return
        if source_type in TICKET_SOURCE_PLATFORMS:
            platform = TICKET_SOURCE_PLATFORMS[source_type]
            decision = _ticket_relevance_rules(resource, platform=platform)
            cached_decision = self.cached_activity_decision(resource)
            if cached_decision and decision["method"] != "music-pilot":
                decision = cached_decision
            elif decision["method"] != "music-pilot":
                llm_decision = self.call_ticket_relevance_llm(
                    resource,
                    platform=platform,
                    rule_decision=decision,
                )
                if llm_decision:
                    decision = llm_decision
            activity_profiles = self.store_activity_profiles(resource, decision)
            self.set_ticket_relevance(content_id, decision)
            source_id = _stable_id("source", resource["source_id"])
            if decision["status"] == "accepted":
                self.upsert_ticket_page(
                    resource,
                    source_id=source_id,
                    platform=platform,
                    activity_profiles=activity_profiles,
                )
            else:
                removed_tickets, removed_events = self.remove_ticket_page_projection(
                    resource,
                    platform=platform,
                )
                LOGGER.info(
                    "ticket relevance=%s platform=%s title=%r reason=%s "
                    "removed_tickets=%s removed_events=%s",
                    decision["status"],
                    platform,
                    resource.get("title"),
                    decision.get("reason"),
                    removed_tickets,
                    removed_events,
                )
            self.remove_news_projection(content_id)
            self.finish(job["id"])
            return
        enrichment = self.call_llm(resource, category)
        if enrichment:
            content_id, _ = self.upsert_content(resource, enrichment=enrichment, category=category)
            for fact in enrichment.get("facts") or []:
                if isinstance(fact, dict) and fact.get("kind") in {"EVENT", "TICKET", "RELEASE"}:
                    self.candidate(content_id, fact)
        self.finish(job["id"])

    def process_once(self) -> bool:
        job = self.claim()
        if not job:
            return False
        try:
            self.process(job)
            LOGGER.info("normalized job=%s resource=%s", job["id"], job["resourceId"])
        except Exception as exc:
            LOGGER.exception("normalization failed job=%s", job["id"])
            self.fail(job, exc)
        return True


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in {"/", "/health"}:
            self.send_response(404)
            self.end_headers()
            return
        body = b'{"service":"genchi-normalizer","ready":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


def run(settings: Settings) -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    server = ThreadingHTTPServer(("0.0.0.0", 8070), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    normalizer = Normalizer(settings)
    LOGGER.info("normalizer started llm_enabled=%s", settings.llm_enabled)
    try:
        while not stop.is_set():
            if not normalizer.process_once():
                stop.wait(settings.poll_seconds)
    finally:
        server.shutdown()
