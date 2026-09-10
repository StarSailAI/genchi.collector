"""Explicit request locale and display-only catalogue projections.

Source text, identifiers, evidence and editorial Chinese names are never rewritten.
English and Japanese use original official names when no reviewed translation exists.
"""

import json
import re
from contextvars import ContextVar
from functools import cache
from importlib.resources import files
from typing import Literal

from opencc import OpenCC
from starlette.responses import JSONResponse

Locale = Literal["zh-Hans", "zh-Hant", "en", "ja"]
LOCALES = ("zh-Hans", "zh-Hant", "en", "ja")
request_locale: ContextVar[str] = ContextVar("genchi_locale", default="zh-Hans")
localize_catalog: ContextVar[bool] = ContextVar("genchi_localize_catalog", default=False)


def locale_of(value: str | None) -> str:
    return value if value in LOCALES else "zh-Hans"


@cache
def converter():
    return OpenCC("s2twp")


def traditional(value: str) -> str:
    return converter().convert(value)


@cache
def messages():
    return json.loads(files("genchi_product").joinpath("data/messages.json").read_text())


def translate(key: str, locale: str | None = None, **values) -> str:
    language = locale_of(locale or request_locale.get())
    index = LOCALES.index(language) - 1
    result = key if index < 0 else messages().get(key, [key] * 3)[index]
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m[1], m[0])), result)


def display_name(original: str | None, chinese: str | None, locale: str) -> str:
    if locale == "zh-Hans":
        return chinese or original or ""
    if locale == "zh-Hant":
        return traditional(chinese) if chinese else original or ""
    return original or chinese or ""


def display_title(row: dict, locale: str) -> str:
    return display_name(row.get("title"), row.get("title_zh"), locale)


def localized_catalog(value, locale: str):
    if isinstance(value, list):
        return [localized_catalog(item, locale) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: localized_catalog(item, locale) for key, item in value.items()}
    for field in ("title", "label", "name"):
        if field in value and field + "_zh" in value:
            result[field + "_localized"] = display_name(value[field], value[field + "_zh"], locale)
    for field in ("activity_title", "milestone_title", "name"):
        if field + "_original" in value:
            result[field] = display_name(value[field + "_original"], value.get(field), locale)
    # Fixed UI labels only. User keywords and original titles remain untouched.
    if value.get("target_type") == "TAG" and "name" in value:
        result["name"] = translate(value["name"], locale)
    if "boundary" in value and "node" in value:
        localized_node = dict(value["node"])
        localized_node["title_zh"] = display_title(localized_node, locale)
        # The API already has serialized dates; match the existing boundary label.
        label = value.get("label", "")
        if label in messages():
            result["label"] = translate(label, locale)
        elif label.endswith(" / 截止"):
            result["label"] = translate(label[:-5], locale) + " " + translate("/ 截止", locale)
        else:
            result["label"] = display_title(localized_node, locale)
    if "slug" in value and value.get("name") in messages() and "name_zh" not in value:
        result["name_localized"] = translate(value["name"], locale)
    # Curated prose remains attributed source material; only Chinese script changes here.
    if locale == "zh-Hant":
        for field in ("summary", "description", "eligibility", "notes"):
            if isinstance(value.get(field), str):
                result[field + "_localized"] = traditional(value[field])
    return result


class LocalizedJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        if localize_catalog.get():
            content = localized_catalog(content, request_locale.get())
        return super().render(content)
