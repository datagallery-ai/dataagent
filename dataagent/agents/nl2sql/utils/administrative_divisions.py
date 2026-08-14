from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from dataagent.utils.log import logger

_INDEX_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "perceptor" / "china_administrative_division_aliases.json"
)
_LEVEL_PRIORITY = {"province": 1, "city": 2, "county": 3}
_RECORD_FIELDS = (
    "level",
    "name",
    "code",
    "province_name",
    "province_code",
    "city_name",
    "city_code",
)


def _normalize(text: str) -> str:
    return re.sub(r"[\s_-]+", " ", str(text or "").casefold()).strip()


@lru_cache(maxsize=1)
def _load_administrative_division_index() -> tuple[
    dict[str, tuple[dict[str, str], ...]],
    re.Pattern[str] | None,
]:
    try:
        payload = json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("unsupported administrative division index version")
        raw_divisions = payload.get("divisions")
        if not isinstance(raw_divisions, list):
            raise ValueError("administrative division index has no divisions list")

        alias_records: dict[str, list[dict[str, str]]] = {}
        identities: set[tuple[str, str]] = set()
        for raw in raw_divisions:
            if not isinstance(raw, dict):
                raise ValueError("administrative division record must be an object")
            record = {field: str(raw.get(field, "")) for field in _RECORD_FIELDS}
            level = record["level"]
            code = record["code"]
            if level not in _LEVEL_PRIORITY or not re.fullmatch(r"\d{6}", code):
                raise ValueError(f"invalid administrative division identity: {level}/{code}")
            if not record["name"] or not re.fullmatch(r"\d{6}", record["province_code"]):
                raise ValueError(f"invalid administrative division parents: {level}/{code}")
            if level in {"city", "county"} and not record["province_name"]:
                raise ValueError(f"missing province parent: {level}/{code}")
            if level in {"city", "county"} and (
                not record["city_name"] or not re.fullmatch(r"\d{6}", record["city_code"])
            ):
                raise ValueError(f"missing city data: {level}/{code}")
            identity = (level, code)
            if identity in identities:
                raise ValueError(f"duplicate administrative division identity: {level}/{code}")
            identities.add(identity)

            raw_aliases = raw.get("aliases")
            if not isinstance(raw_aliases, list) or not raw_aliases:
                raise ValueError(f"missing administrative division aliases: {level}/{code}")
            for raw_alias in raw_aliases:
                alias = _normalize(str(raw_alias))
                if not alias:
                    continue
                records = alias_records.setdefault(alias, [])
                if record not in records:
                    records.append(record)

        if not alias_records:
            raise ValueError("administrative division index has no aliases")
        ordered_aliases = sorted(alias_records, key=lambda value: (-len(value), value))
        pattern_parts = []
        for alias in ordered_aliases:
            escaped = re.escape(alias)
            if re.fullmatch(r"[a-z0-9 ]+", alias):
                escaped = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
            pattern_parts.append(escaped)
        pattern = re.compile("|".join(pattern_parts))
        return {alias: tuple(records) for alias, records in alias_records.items()}, pattern
    except (OSError, ValueError, TypeError, re.error) as exc:
        logger.warning(f"Administrative division index unavailable: {exc}")
        return {}, None


def match_administrative_divisions(question: str) -> list[dict[str, str]]:
    """Return administrative divisions mentioned in the user question."""

    alias_records, pattern = _load_administrative_division_index()
    normalized_question = _normalize(question)
    if not normalized_question or pattern is None:
        return []

    groups: list[list[dict[str, str]]] = []
    for match in pattern.finditer(normalized_question):
        candidates = list(alias_records[_normalize(match.group(0))])
        max_priority = max(_LEVEL_PRIORITY[item["level"]] for item in candidates)
        groups.append([item for item in candidates if _LEVEL_PRIORITY[item["level"]] == max_priority])

    province_context: set[str] = set()
    city_context: set[str] = set()
    for candidates in groups:
        for item in candidates:
            if item["level"] == "province":
                province_context.add(item["code"])
            elif item["level"] == "city":
                city_context.add(item["code"])

    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidates in groups:
        if len(candidates) > 1 and candidates[0]["level"] == "county":
            contextual = [
                item
                for item in candidates
                if item["city_code"] in city_context or item["province_code"] in province_context
            ]
            candidates = contextual or candidates
        elif len(candidates) > 1 and candidates[0]["level"] == "city":
            contextual = [item for item in candidates if item["province_code"] in province_context]
            candidates = contextual or candidates

        for item in sorted(
            candidates,
            key=lambda value: (value["province_code"], value["city_code"], value["code"]),
        ):
            identity = (item["level"], item["code"])
            if identity in seen:
                continue
            seen.add(identity)
            results.append(dict(item))
    return results


def format_administrative_division_rules(question: str) -> str:
    """Format matched administrative divisions as SQL generation rules."""

    matches = match_administrative_divisions(question)
    if not matches:
        return ""

    lines = ["", "", "## 行政区划匹配"]
    for item in matches:
        parents = []
        if item["level"] in {"city", "county"} and item["province_name"] != item["name"]:
            parents.append(item["province_name"])
        if item["level"] == "county" and item["city_name"] != item["name"] and item["city_name"] not in parents:
            parents.append(item["city_name"])
        parent_text = f"（{' / '.join(parents)}）" if parents else ""
        lines.append(f"- {item['name']}：{item['level']}，行政编码为 {item['code']}{parent_text}")
    return "\n".join(lines)
