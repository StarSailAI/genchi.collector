"""Bounded catalogue Q&A. The model never receives SQL tools or account data."""

from __future__ import annotations

import json
import os
from typing import Literal
from urllib.parse import urlsplit

import psycopg
import requests
from fastapi import HTTPException, Request, Response
from genchi_normalizer.glossary import glossary_prompt
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import rate_limit, source_key
from .localization import request_locale


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=2, max_length=400)

    @field_validator("question")
    @classmethod
    def clean(cls, value):
        value = value.strip()
        if len(value) < 2 or any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError("Invalid question")
        return value


class SearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relevant: bool
    keywords: list[str] = Field(max_length=4)
    subjects: list[str] = Field(max_length=4)
    kind: Literal["", "LIVE", "FESTIVAL", "POPUP", "CAFE", "EXHIBITION", "MEETUP", "GOODS", "OTHER"] = ""
    upcoming: bool = True

    @field_validator("keywords", "subjects")
    @classmethod
    def bounded(cls, values):
        if any(not 1 <= len(v.strip()) <= 80 for v in values):
            raise ValueError("Invalid search term")
        return [v.strip() for v in values]


class GroundedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str = Field(min_length=1, max_length=5000)
    source_ids: list[str] = Field(max_length=8)
    found: bool


def model_config():
    base = os.getenv("ASK_LLM_BASE_URL", "").strip()
    key = os.getenv("ASK_LLM_API_KEY", "").strip()
    model = os.getenv("ASK_LLM_MODEL", "deepseek-flash").strip()
    if not base or not key or not model or urlsplit(base).scheme != "https":
        raise HTTPException(503, "问答暂时不可用，请先浏览下方活动。")
    endpoint = base.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions" if endpoint.endswith("/v1") else "/v1/chat/completions"
    return endpoint, key, model


def complete(config, instruction, data, max_tokens):
    endpoint, key, model = config
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": instruction + glossary_prompt()},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False, default=str)},
        ],
        "temperature": 0, "response_format": {"type": "json_object"}, "max_tokens": max_tokens,
        **({"thinking": {"type": "disabled"}} if urlsplit(endpoint).hostname == "api.deepseek.com" else {}),
    }
    # No retries: one accepted question makes at most two bounded model requests.
    with requests.post(endpoint, headers={"Authorization": f"Bearer {key}"}, json=payload,
                       timeout=(5, 25), stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        raw = bytearray()
        for chunk in response.iter_content(8192):
            raw.extend(chunk)
            if len(raw) > 100000:
                raise ValueError("Model response too large")
        choice = json.loads(raw)["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("Incomplete model output")
    return json.loads(choice["message"]["content"])


def retrieve(conn, plan):
    clauses = ["a.publication='PUBLISHED'", "a.attendance IN ('OFFLINE','HYBRID')"]
    params = []
    if plan.keywords:
        patterns = ["%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                    for word in plan.keywords]
        clauses.append("""(a.title ILIKE ANY(%s) OR a.title_zh ILIKE ANY(%s)
          OR EXISTS(SELECT 1 FROM catalog_activity_subjects l JOIN catalog_subjects s ON s.slug=l.subject_slug
            WHERE l.activity_id=a.id AND (l.participant_name ILIKE ANY(%s)
              OR s.name ILIKE ANY(%s) OR s.name_zh ILIKE ANY(%s) OR s.aliases::text ILIKE ANY(%s))))""")
        params.extend([patterns] * 6)
    if plan.subjects:
        clauses.append("""a.id IN (WITH RECURSIVE descendants AS (
            SELECT slug FROM catalog_subjects WHERE slug=ANY(%s) UNION
            SELECT s.slug FROM catalog_subjects s JOIN descendants d ON s.parent_slug=d.slug)
            SELECT activity_id FROM catalog_activity_subjects WHERE subject_slug IN (SELECT slug FROM descendants))""")
        params.append(plan.subjects)
    if plan.kind:
        clauses.append("a.kind=%s")
        params.append(plan.kind)
    if plan.upcoming:
        clauses.append("""(EXISTS(SELECT 1 FROM catalog_occurrences o WHERE o.activity_id=a.id
          AND o.status<>'SUPERSEDED' AND (o.precision='TBD' OR COALESCE(o.ends_at,
            (o.ends_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo',o.starts_at,
            (o.starts_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo')>NOW()))
          OR EXISTS(SELECT 1 FROM catalog_milestones m WHERE m.activity_id=a.id AND m.status='CONFIRMED'
            AND COALESCE(m.ends_at,(m.ends_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo',m.starts_at,
            (m.starts_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo')>NOW()))""")
    rows = conn.execute("""SELECT a.id,a.title,a.title_zh,a.kind,a.summary,a.status,a.official_url,a.updated_at,
        (SELECT min(COALESCE(o.starts_at,o.starts_on::timestamp AT TIME ZONE 'Asia/Tokyo'))
         FROM catalog_occurrences o WHERE o.activity_id=a.id AND o.status<>'SUPERSEDED'
           AND COALESCE(o.ends_at,(o.ends_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo',o.starts_at,
           (o.starts_on+1)::timestamp AT TIME ZONE 'Asia/Tokyo')>NOW()) AS next_at
        FROM catalog_activities a WHERE """ + " AND ".join(clauses)
        + " ORDER BY next_at NULLS LAST,a.updated_at DESC,a.id LIMIT 17", params).fetchall()
    truncated = len(rows) > 16
    rows = rows[:16]
    for row in rows:
        row["occurrences"] = conn.execute("""SELECT id,label,venue,city,precision,starts_at,ends_at,
            starts_on,ends_on,status FROM catalog_occurrences WHERE activity_id=%s AND status<>'SUPERSEDED'
            ORDER BY COALESCE(starts_at,starts_on::timestamp AT TIME ZONE 'Asia/Tokyo') DESC LIMIT 40""",
            (row["id"],)).fetchall()
        row["milestones"] = conn.execute("""SELECT m.id,m.title,m.title_zh,m.kind,m.status,m.precision,m.starts_at,m.ends_at,
            m.starts_on,m.ends_on,m.round_key,m.eligibility,m.url,
            EXISTS(SELECT 1 FROM catalog_evidence e WHERE e.milestone_id=m.id AND e.verified) AS verified,
            ARRAY(SELECT occurrence_id FROM catalog_milestone_scopes WHERE milestone_id=m.id) AS occurrence_ids
            FROM catalog_milestones m WHERE m.activity_id=%s AND m.status<>'SUPERSEDED'
            ORDER BY COALESCE(m.ends_at,m.ends_on::timestamp AT TIME ZONE 'Asia/Tokyo',m.starts_at,
              m.starts_on::timestamp AT TIME ZONE 'Asia/Tokyo') DESC NULLS LAST LIMIT 40""", (row["id"],)).fetchall()
        row["relationships"] = conn.execute("""SELECT subject_slug,relation_kind,participant_name,scope_note,verified
            FROM catalog_activity_subjects WHERE activity_id=%s ORDER BY subject_slug LIMIT 20""",
            (row["id"],)).fetchall()
        row["evidence"] = conn.execute("""SELECT url,left(excerpt,1200) AS excerpt,verified,observed_at,milestone_id
            FROM catalog_evidence WHERE activity_id=%s ORDER BY verified DESC,observed_at DESC LIMIT 6""",
            (row["id"],)).fetchall()
        row["summary"] = (row["summary"] or "")[:1200]
    return rows, truncated


def grounded_context(rows):
    """Give the model short source handles and only evidence-backed time values."""
    context = json.loads(json.dumps(rows, default=str))
    sources = {}
    for index, row in enumerate(context, 1):
        handle = f"A{index}"
        sources[handle] = rows[index - 1]
        row["id"] = handle
        row.pop("next_at", None)
        verified_occurrences = {
            key for node in row["milestones"] if node["verified"]
            and node["kind"] in {"START", "PERIOD"} and node["status"] == "CONFIRMED"
            for key in node["occurrence_ids"]
        }
        occurrence_ids = {node["id"]: f"{handle}-O{i}" for i, node in enumerate(row["occurrences"], 1)}
        for node in row["occurrences"]:
            node["verified"] = node["id"] in verified_occurrences
            node["id"] = occurrence_ids[node["id"]]
        for i, node in enumerate(row["milestones"], 1):
            node["id"] = f"{handle}-M{i}"
            node["occurrence_ids"] = [occurrence_ids[key] for key in node["occurrence_ids"] if key in occurrence_ids]
        for node in [*row["occurrences"], *row["milestones"]]:
            if not node["verified"]:
                for field in ("starts_at", "ends_at", "starts_on", "ends_on"):
                    node[field] = None
                node["precision"] = "TBD"
                node["time_unverified"] = True
        # Unreviewed excerpts can otherwise reintroduce dates masked above.
        row["evidence"] = [{k: v for k, v in proof.items() if k != "milestone_id"}
                           for proof in row["evidence"] if proof["verified"]]
        if not row["evidence"]:
            row["summary"] = ""
    return context, sources


def answer_question(catalog, question):
    config = model_config()
    # GLOBAL means all visitors AND all logged-in accounts, on every process/host.
    # Commit before any model/network work; failures retain the slot to bound cost.
    try:
        rate_limit(catalog, [("homepage-ask-global", "all-users", 3600, 1)])
    except HTTPException as exc:
        if exc.status_code == 429:
            # Concurrent transactions can start before the winning transaction's
            # clock, so the generic counter may round its retry to 3601 seconds.
            retry = max(1, min(3600, int((exc.headers or {}).get("Retry-After", "3600"))))
            raise HTTPException(429, "免费问答通道拥挤，请稍后再试。",
                                headers={"Retry-After": str(retry)}) from None
        raise
    from .ask_agent import run_agent

    try:
        return run_agent(catalog, question, config, locale=request_locale.get())
    except (requests.RequestException, psycopg.Error, ValueError, KeyError, TypeError, IndexError, TimeoutError):
        raise HTTPException(503, "问答暂时不可用，请先浏览下方活动。") from None


def register_assistant_routes(app, catalog):
    @app.post("/ask")
    def ask(body: Question, request: Request, response: Response):
        # Authenticate BFF attribution. The product port is private; unknown callers fail closed.
        if not request.headers.get("X-Genchi-Client-IP"):
            raise HTTPException(403, "请求来源验证失败")
        source_key(request)
        response.headers["Cache-Control"] = "no-store"
        return answer_question(catalog, body.question)

    @app.get("/home/featured")
    def home_featured(response: Response):
        from .home import featured

        response.headers["Cache-Control"] = "no-store"
        return featured(catalog)
