"""Chinese display names are editorial data, never identity or event revisions.

Source text stays byte-for-byte intact. Only reviewed exact names and conservative
glossary substitutions are automatic; unfamiliar Japanese names enter name review.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from genchi_normalizer.glossary import load_glossary
from psycopg.types.json import Jsonb

TABLES = {
    "ACTIVITY": ("catalog_activities", "id", "title", "title_zh"),
    "MILESTONE": ("catalog_milestones", "id", "title", "title_zh"),
    "OCCURRENCE": ("catalog_occurrences", "id", "label", "label_zh"),
    "SUBJECT": ("catalog_subjects", "slug", "name", "name_zh"),
}


def spacing(value: str) -> str:
    # Width normalization is for matching / display only, not stored source text.
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


@cache
def policy():
    return load_glossary()


@cache
def reviewed_names():
    path = files("genchi_product").joinpath("data/reviewed-names.json")
    return json.loads(path.read_text()) if path.is_file() else {"title": {}, "flow": {}}


@cache
def glossary():
    terms = {spacing(k): v for k, v in policy()["terms"].items()}

    # One pass, longest match first: replacing a child subject must not translate it twice.
    def expression(term):
        left = r"(?<![A-Za-z0-9])" if re.match(r"[A-Za-z0-9]", term) else ""
        right = r"(?![A-Za-z0-9])" if re.search(r"[A-Za-z0-9]$", term) else ""
        return left + re.escape(term) + right

    pattern = re.compile("|".join(expression(k) for k in sorted(terms, key=len, reverse=True)))
    return pattern, terms


@dataclass(frozen=True)
class Name:
    text: str
    state: str
    method: str


def normalize_name(source: str, entity_type: str, entity_id: str = "") -> Name:
    if entity_type not in TABLES:
        raise ValueError("Unknown name entity")
    if entity_type == "SUBJECT" and entity_id in policy()["subjects"]:
        return Name(policy()["subjects"][entity_id], "NORMALIZED", "subject-glossary")
    kind = "flow" if entity_type == "MILESTONE" else "title"
    exact = reviewed_names().get(kind, {}).get(spacing(source))
    if exact:
        return Name(exact, "NORMALIZED", "reviewed-catalog")
    pattern, terms = glossary()
    text = pattern.sub(lambda m: terms[m[0]], spacing(source))
    if entity_type == "MILESTONE":
        # Provider stars/arrows are decoration; seat classes and round numbers are facts.
        text = re.sub(r"[★☆◆◇]", "", text).strip()
        text = re.sub(r"(?<!\d)(\d+)次", r"第\1轮", text)
        for mode in ("先到先得", "抽选"):
            if text.startswith(mode + " "):
                text = text[len(mode) :].strip()
                if mode not in text:
                    text += f"（{mode}）"
        text = re.sub(r" +", " ", text)
    remaining = text
    for term in sorted(policy()["protected"], key=len, reverse=True):
        remaining = remaining.replace(term, "")
    uncertain = bool(re.search(r"[\u3040-\u30ff]|https?://|[━╳◢◣]", remaining))
    return Name(text or source, "REVIEW" if uncertain else "NORMALIZED", "glossary")


def sync_name(
    conn,
    entity_type: str,
    entity_id: str,
    *,
    approved: str | None = None,
    expected_source: str | None = None,
    apply: bool = True,
):
    table, key, source_field, display_field = TABLES[entity_type]
    row = conn.execute(
        f"SELECT {source_field} AS source,{display_field} AS display FROM {table} WHERE {key}=%s FOR UPDATE",
        (entity_id,),
    ).fetchone()
    if not row or row["source"] is None:
        return None
    source = row["source"]
    if expected_source is not None and source != expected_source:
        raise ValueError("原始名称已更新，请重新核对")
    prior = conn.execute(
        "SELECT * FROM catalog_names WHERE entity_type=%s AND entity_id=%s FOR UPDATE",
        (entity_type, entity_id),
    ).fetchone()
    if approved is not None:
        display = spacing(approved)
        if not display or len(display) > 1000:
            raise ValueError("展示名称须为 1 至 1000 字")
        result = Name(display, "NORMALIZED", "editor")
    elif prior and prior["method"] == "editor" and prior["source_text"] == source:
        result = Name(prior["display_text"], prior["state"], "editor")
    else:
        result = normalize_name(source, entity_type, entity_id)
    changed = row["display"] != result.text
    metadata_changed = not prior or any(
        prior[k] != v
        for k, v in {
            "source_text": source,
            "display_text": result.text,
            "state": result.state,
            "method": result.method,
            "policy_version": policy()["version"],
        }.items()
    )
    report = dict(
        entity_type=entity_type,
        entity_id=entity_id,
        source=source,
        before=row["display"],
        display=result.text,
        state=result.state,
        method=result.method,
        changed=changed,
        metadata_changed=metadata_changed,
    )
    if apply and (changed or metadata_changed):
        conn.execute(
            f"UPDATE {table} SET {display_field}=%s WHERE {key}=%s", (result.text, entity_id)
        )
        conn.execute(
            """INSERT INTO catalog_names(entity_type,entity_id,source_text,display_text,state,method,policy_version)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(entity_type,entity_id) DO UPDATE SET
            source_text=EXCLUDED.source_text,display_text=EXCLUDED.display_text,state=EXCLUDED.state,
            method=EXCLUDED.method,policy_version=EXCLUDED.policy_version,updated_at=NOW()""",
            (
                entity_type,
                entity_id,
                source,
                result.text,
                result.state,
                result.method,
                policy()["version"],
            ),
        )
        conn.execute(
            """INSERT INTO catalog_name_history(entity_type,entity_id,source_text,before_text,after_text,state,method,policy_version)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                entity_type,
                entity_id,
                source,
                row["display"],
                result.text,
                result.state,
                result.method,
                policy()["version"],
            ),
        )
        if entity_type == "SUBJECT":
            # Keep old spellings searchable and usable by subject matching.
            conn.execute(
                """UPDATE catalog_subjects SET aliases=(SELECT jsonb_agg(DISTINCT value)
                FROM jsonb_array_elements(aliases || %s::jsonb)) WHERE slug=%s""",
                (Jsonb([v for v in (source, row["display"], result.text) if v]), entity_id),
            )
    return report


def normalize_catalog(catalog, *, apply: bool = False) -> dict:
    reports = []
    with catalog.connect() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('genchi:names',0))")
        for kind, (table, key, source, _) in TABLES.items():
            for row in conn.execute(
                f"SELECT {key} FROM {table} WHERE {source} IS NOT NULL ORDER BY {key}"
            ).fetchall():
                reports.append(sync_name(conn, kind, row[key], apply=apply))
    return {
        "applied": apply,
        "policy_version": policy()["version"],
        "total": len(reports),
        "changed": sum(r["changed"] for r in reports),
        "review": sum(r["state"] == "REVIEW" for r in reports),
        "entities": {kind: sum(r["entity_type"] == kind for r in reports) for kind in TABLES},
        "items": reports,
    }


def title(row: dict) -> str:
    return row.get("title_zh") or row["title"]


def change_summary(value: str) -> str:
    for prefix in ("新增：", "更新："):
        if value.startswith(prefix):
            return prefix + normalize_name(value[len(prefix) :], "MILESTONE").text
    return value
