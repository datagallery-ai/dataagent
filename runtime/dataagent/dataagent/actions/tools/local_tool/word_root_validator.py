# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Validate names against a CSV word-root table and retrieve semantic candidates."""

from __future__ import annotations

import asyncio
import csv
import copy
import hashlib
import io
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from loguru import logger

_PROMPT_VERSION = "word-root-candidate-ranking-full-table-topk-v3"
_MAX_K = 20
_MAX_BATCH_QUERY_ITEMS = 20
_DEFAULT_LLM_BATCH_SIZE = 5
_RESULT_CACHE_LIMIT = 256

_result_cache: OrderedDict[tuple[str, str, str, int, str, str, str], dict[str, Any]] = OrderedDict()
_result_cache_lock = threading.RLock()


@dataclass(frozen=True)
class _WordRootRecord:
    row_id: str
    row_number: int
    values: dict[str, str]
    english_name: str
    chinese_description: str
    is_special: bool
    compiled_pattern: re.Pattern[str] | None


@dataclass(frozen=True)
class _WordRootTable:
    fingerprint: str
    records: tuple[_WordRootRecord, ...]
    english_column: str
    chinese_name_column: str
    description_column: str
    type_column: str
    regex_column: str


async def validate_word_root(
    english_name: str,
    chinese_description: str | None = None,
    k: int = 5,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Check an English name against a configured CSV word-root table.

    Exact normalized matches and special-root regular expressions are evaluated
    locally. For unresolved names, the configured LLM ranks candidates from the
    full table in one request.

    Args:
        english_name: English name to validate.
        chinese_description: Optional Chinese description used for semantic candidate search.
        k: Maximum number of candidate records to return (1-20, default 5).
        _tool_context: Injected DataAgent context. Configure this tool with
            ``AGENT_CONFIG.word_roots.csv_path`` for the dictionary path; CSV column
            names remain in the tool YAML ``config``. Legacy ``config.csv_path`` is supported.

    Returns:
        A JSON-serializable result containing exact/regex matches or ranked candidates.
        If the CSV file is missing, returns ``validation_status="skipped"`` without
        blocking the caller.
    """
    english_name = english_name.strip()
    if not english_name:
        raise ValueError("english_name must not be empty")
    if chinese_description is not None and not isinstance(chinese_description, str):
        raise ValueError("chinese_description must be a string or None")
    chinese_description = (chinese_description or "").strip()
    if not 1 <= k <= _MAX_K:
        raise ValueError(f"k must be between 1 and {_MAX_K}")

    config = _word_root_config(_tool_context)
    try:
        table = _load_word_root_table(config)
    except FileNotFoundError as exc:
        logger.warning("Word-root CSV is missing; skipping validation: {}", exc)
        return _validation_skipped_result()
    llm_call_params = _llm_call_params(config)
    normalized_name = _normalize_name(english_name)

    exact_matches = [
        record for record in table.records
        if not record.is_special and _normalize_name(record.english_name) == normalized_name
    ]
    if exact_matches:
        return _matched_result("exact", exact_matches)

    regex_matches = [
        record for record in table.records
        if record.is_special
        and record.compiled_pattern is not None
        and record.compiled_pattern.fullmatch(english_name)
    ]
    if regex_matches:
        return _matched_result("regex", regex_matches)

    cache_key = _candidate_cache_key(
        table=table,
        english_name=normalized_name,
        chinese_description=chinese_description,
        k=k,
        config=config,
        llm_call_params=llm_call_params,
    )
    cached = _get_cached_result(cache_key)
    if cached is not None:
        return cached

    normal_records = [record for record in table.records if not record.is_special]
    model_name = str(config.get("llm_model") or "planner").strip()
    runtime = _tool_context.runtime
    if runtime is not None:
        llm = runtime.llm(model_name)
    else:
        from dataagent.core.managers.llm_manager import llm_manager

        llm = llm_manager.get_llm(model_name) or llm_manager.get_default_llm()

    candidate_records = await _rank_candidates(
        llm=llm,
        records=normal_records,
        english_name=english_name,
        chinese_description=chinese_description,
        k=k,
        llm_call_params=llm_call_params,
    )
    candidates = [
        {"record": record.values, "relevance": score}
        for record, score in candidate_records
    ]
    result = {
        "found": False,
        "match_type": None,
        "matches": [],
        "candidates": candidates,
    }
    _put_cached_result(cache_key, result)
    return result


async def validate_word_roots(
    items: list[dict[str, str | None]],
    k: int = 5,
    llm_batch_size: int = _DEFAULT_LLM_BATCH_SIZE,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Validate a batch of proposed word roots against the configured table.

    Each input item requires ``english_name`` and may contain ``chinese_description``.
    Exact and special-regex checks are local. Unresolved items are sent to the
    model in groups, with the full table included once per request. Duplicate
    items are processed once and their results are expanded back to original
    input order.

    Args:
        items: Non-empty list of objects with non-empty ``english_name`` strings and
            optional ``chinese_description`` strings. Identical normalized pairs are
            deduplicated; at most 20 unique pairs are allowed.
        k: Maximum number of candidates to return for each input (1-20, default 5).
        llm_batch_size: Maximum unresolved items sent in one LLM request (1-20, default 5).
        _tool_context: Injected DataAgent context; callers must not supply it.

    Returns:
        An object with a ``results`` array, one validation result per original input item.
        If the CSV file is missing, returns ``validation_status="skipped"`` and
        marks each item's ``found`` as null without blocking the caller.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= _MAX_K:
        raise ValueError(f"k must be an integer between 1 and {_MAX_K}")
    if (
        not isinstance(llm_batch_size, int)
        or isinstance(llm_batch_size, bool)
        or not 1 <= llm_batch_size <= _MAX_BATCH_QUERY_ITEMS
    ):
        raise ValueError(f"llm_batch_size must be an integer between 1 and {_MAX_BATCH_QUERY_ITEMS}")

    unique_items: list[dict[str, str]] = []
    unique_index_by_identity: dict[tuple[str, str], int] = {}
    original_to_unique_index: list[int] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{index}] must be an object")
        english_name = item.get("english_name")
        chinese_description = item.get("chinese_description")
        if not isinstance(english_name, str) or not english_name.strip():
            raise ValueError(f"items[{index}].english_name must be a non-empty string")
        if chinese_description is not None and not isinstance(chinese_description, str):
            raise ValueError(f"items[{index}].chinese_description must be a string when provided")
        normalized_item = {
            "english_name": english_name.strip(),
            "chinese_description": (chinese_description or "").strip(),
        }
        identity = (normalized_item["english_name"], normalized_item["chinese_description"])
        unique_index = unique_index_by_identity.get(identity)
        if unique_index is None:
            unique_index = len(unique_items)
            unique_index_by_identity[identity] = unique_index
            unique_items.append(normalized_item)
        original_to_unique_index.append(unique_index)

    if len(unique_items) > _MAX_BATCH_QUERY_ITEMS:
        raise ValueError(
            f"items must contain no more than {_MAX_BATCH_QUERY_ITEMS} unique entries"
        )

    config = _word_root_config(_tool_context)
    try:
        table = _load_word_root_table(config)
    except FileNotFoundError as exc:
        logger.warning("Word-root CSV is missing; skipping batch validation: {}", exc)
        skipped_items = []
        for original_index, unique_index in enumerate(original_to_unique_index):
            skipped_items.append({
                "index": original_index,
                **unique_items[unique_index],
                **_validation_skipped_item_result(),
            })
        return {
            "validation_status": "skipped",
            "reason": "word_root_csv_missing",
            "results": skipped_items,
        }
    llm_call_params = _llm_call_params(config)
    results: list[dict[str, Any] | None] = [None] * len(unique_items)
    pending: list[dict[str, Any]] = []

    for index, item in enumerate(unique_items):
        english_name = item["english_name"]
        chinese_description = item["chinese_description"]
        match_result = _local_match_result(table, english_name)
        if match_result is not None:
            results[index] = {
                "index": index,
                **item,
                **match_result,
            }
            continue

        cache_key = _candidate_cache_key(
            table=table,
            english_name=english_name,
            chinese_description=chinese_description,
            k=k,
            config=config,
            llm_call_params=llm_call_params,
        )
        cached = _get_cached_result(cache_key)
        if cached is not None:
            results[index] = {"index": index, **item, **cached}
            continue
        pending.append({
            "query_id": f"q{index}",
            "index": index,
            **item,
            "cache_key": cache_key,
        })

    if pending:
        normal_records = [record for record in table.records if not record.is_special]
        model_name = str(config.get("llm_model") or "planner").strip()
        runtime = _tool_context.runtime
        if runtime is not None:
            llm = runtime.llm(model_name)
        else:
            from dataagent.core.managers.llm_manager import llm_manager

            llm = llm_manager.get_llm(model_name) or llm_manager.get_default_llm()

        pools: dict[str, list[tuple[_WordRootRecord, float]]] = {}
        for offset in range(0, len(pending), llm_batch_size):
            query_batch = pending[offset : offset + llm_batch_size]
            pools.update(await _rank_candidate_queries(
                llm=llm,
                records=normal_records,
                queries=query_batch,
                k=k,
                llm_call_params=llm_call_params,
            ))
        ranked_candidates = {
            query_id: [
                {"record": record.values, "relevance": score}
                for record, score in pools.get(query_id, [])[:k]
            ]
            for query_id in pools
        }

        for query in pending:
            query_id = query["query_id"]
            candidates = ranked_candidates.get(query_id, [])
            result = {
                "found": False,
                "match_type": None,
                "matches": [],
                "candidates": candidates,
            }
            _put_cached_result(query["cache_key"], result)
            results[query["index"]] = {
                "index": query["index"],
                "english_name": query["english_name"],
                "chinese_description": query["chinese_description"],
                **result,
            }

    unique_results = [result for result in results if result is not None]
    expanded_results = []
    for original_index, unique_index in enumerate(original_to_unique_index):
        result = copy.deepcopy(unique_results[unique_index])
        result["index"] = original_index
        expanded_results.append(result)
    return {"results": expanded_results}


def _local_match_result(table: _WordRootTable, english_name: str) -> dict[str, Any] | None:
    normalized_name = _normalize_name(english_name)
    exact_matches = [
        record for record in table.records
        if not record.is_special and _normalize_name(record.english_name) == normalized_name
    ]
    if exact_matches:
        return _matched_result("exact", exact_matches)

    regex_matches = [
        record for record in table.records
        if record.is_special
        and record.compiled_pattern is not None
        and record.compiled_pattern.fullmatch(english_name)
    ]
    if regex_matches:
        return _matched_result("regex", regex_matches)
    return None


def _candidate_cache_key(
    *,
    table: _WordRootTable,
    english_name: str,
    chinese_description: str,
    k: int,
    config: dict[str, Any],
    llm_call_params: dict[str, Any],
) -> tuple[str, str, str, int, str, str, str]:
    model_name = str(config.get("llm_model") or "planner").strip()
    version = _PROMPT_VERSION
    return (
        table.fingerprint,
        _normalize_name(english_name),
        _normalize_name(chinese_description),
        k,
        model_name,
        version,
        json.dumps(llm_call_params, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _llm_call_params(config: dict[str, Any]) -> dict[str, Any]:
    if "llm_call_params" not in config:
        return {"enable_thinking": False}
    params = config["llm_call_params"]
    if not isinstance(params, dict) or any(not isinstance(key, str) or not key.strip() for key in params):
        raise ValueError("llm_call_params must be a mapping with non-empty string keys")
    try:
        json.dumps(params, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("llm_call_params values must be JSON-serializable") from exc
    return params


def _word_root_config(context: ToolExecutionContext) -> dict[str, Any]:
    """Override only the CSV path from deployment configuration; keep tool defaults."""
    config = dict(context.tool_config or {})
    if context.config_manager is not None:
        csv_path = str(context.config_manager.get("AGENT_CONFIG.word_roots.csv_path", "") or "").strip()
        if csv_path:
            config["csv_path"] = csv_path
    return config


def _load_word_root_table(config: dict[str, Any]) -> _WordRootTable:
    configured_path = str(config.get("csv_path") or "").strip()
    if not configured_path:
        configured_path = str(Path(__file__).resolve().parents[6] / "word_dict_list.csv")
    path = Path(configured_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"word-root CSV does not exist: {path}")

    english_column = str(config.get("english_name_column") or "词根英文名")
    chinese_name_column = str(config.get("chinese_name_column") or "词根中文名")
    description_column = str(config.get("description_column") or "描述")
    type_column = str(config.get("type_column") or "词根类型")
    regex_column = str(config.get("regex_column") or "特殊词根规则")
    special_value = str(config.get("special_type_value") or "特殊词根").strip().casefold()
    delimiter = str(config.get("delimiter") or "")

    stat = path.stat()
    return _load_word_root_table_cached(
        str(path),
        stat.st_mtime_ns,
        stat.st_size,
        english_column,
        chinese_name_column,
        description_column,
        type_column,
        regex_column,
        special_value,
        delimiter,
    )


@lru_cache(maxsize=8)
def _load_word_root_table_cached(
    path: str,
    _mtime_ns: int,
    _size: int,
    english_column: str,
    chinese_name_column: str,
    description_column: str,
    type_column: str,
    regex_column: str,
    special_value: str,
    configured_delimiter: str,
) -> _WordRootTable:
    raw_bytes = Path(path).read_bytes()
    fingerprint = hashlib.sha256(raw_bytes).hexdigest()
    text = raw_bytes.decode("utf-8-sig")
    delimiter = configured_delimiter or csv.Sniffer().sniff(text[:8192], delimiters=",\t;|").delimiter
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    headers = set(reader.fieldnames or [])
    required_headers = {english_column, chinese_name_column, description_column, type_column, regex_column}
    missing_headers = sorted(required_headers - headers)
    if missing_headers:
        raise ValueError(f"word-root CSV is missing required columns: {', '.join(missing_headers)}")

    records: list[_WordRootRecord] = []
    for row_number, raw_row in enumerate(reader, start=2):
        values = {str(key): str(value or "") for key, value in raw_row.items() if key is not None}
        english = values.get(english_column, "").strip()
        chinese_name = values.get(chinese_name_column, "").strip()
        description = values.get(description_column, "").strip()
        candidate_description = "；".join(
            part for part in (f"词根中文名：{chinese_name}" if chinese_name else "", description) if part
        )
        is_special = values.get(type_column, "").strip().casefold() == special_value
        pattern: re.Pattern[str] | None = None
        if is_special:
            expression = values.get(regex_column, "").strip()
            if not expression:
                raise ValueError(f"special word-root row {row_number} has an empty regex")
            try:
                pattern = _compile_special_pattern(expression)
            except re.error as exc:
                raise ValueError(f"invalid regex in word-root CSV row {row_number}: {exc}") from exc
        records.append(_WordRootRecord(
            row_id=f"row-{row_number}",
            row_number=row_number,
            values=values,
            english_name=english,
            chinese_description=candidate_description,
            is_special=is_special,
            compiled_pattern=pattern,
        ))

    return _WordRootTable(
        fingerprint=fingerprint,
        records=tuple(records),
        english_column=english_column,
        chinese_name_column=chinese_name_column,
        description_column=description_column,
        type_column=type_column,
        regex_column=regex_column,
    )


def _compile_special_pattern(expression: str) -> re.Pattern[str]:
    """Compile either a Python pattern or a slash-delimited regex literal."""
    pattern = expression
    flags = 0
    if expression.startswith("/"):
        closing_slash = expression.rfind("/")
        if closing_slash > 0:
            suffix = expression[closing_slash + 1 :]
            flag_map = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}
            if all(char in flag_map for char in suffix):
                pattern = expression[1:closing_slash]
                for char in suffix:
                    flags |= flag_map[char]
    return re.compile(pattern, flags)


async def _rank_candidates(
    *,
    llm: Any,
    records: list[_WordRootRecord],
    english_name: str,
    chinese_description: str,
    k: int,
    llm_call_params: dict[str, Any],
) -> list[tuple[_WordRootRecord, float]]:
    if not records:
        return []
    query = {
        "query_id": "q0",
        "english_name": english_name,
        "chinese_description": chinese_description,
    }
    pools = await _rank_candidate_queries(
        llm=llm,
        records=records,
        queries=[query],
        k=k,
        llm_call_params=llm_call_params,
    )
    return pools.get("q0", [])[:k]


async def _rank_candidate_queries(
    *,
    llm: Any,
    records: list[_WordRootRecord],
    queries: list[dict[str, Any]],
    k: int,
    llm_call_params: dict[str, Any],
) -> dict[str, list[tuple[_WordRootRecord, float]]]:
    """Send the full ordinary-root table once and retrieve final Top-k results."""
    if not records:
        return {query["query_id"]: [] for query in queries}
    payload = await _invoke_json(
        llm,
        _batch_messages_for_queries(records, queries, k),
        llm_call_params=llm_call_params,
    )
    records_by_query = {query["query_id"]: {} for query in queries}
    query_results = payload.get("results")
    if not isinstance(query_results, list):
        raise ValueError("word-root full-table response must contain a results array")
    records_by_id = {record.row_id: record for record in records}
    for query_result in query_results:
        if not isinstance(query_result, dict):
            continue
        query_id = str(query_result.get("query_id") or "")
        ranked_for_query = records_by_query.get(query_id)
        candidates = query_result.get("candidates")
        if ranked_for_query is None or not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            row_id = str(candidate.get("id") or "")
            record = records_by_id.get(row_id)
            if record is not None and row_id not in ranked_for_query and len(ranked_for_query) < k:
                ranked_for_query[row_id] = (record, _score(candidate.get("score")))

    return {
        query_id: sorted(ranked.values(), key=lambda item: item[1], reverse=True)
        for query_id, ranked in records_by_query.items()
    }


def _batch_messages_for_queries(
    records: list[_WordRootRecord],
    queries: list[dict[str, Any]],
    k: int,
) -> list[dict[str, str]]:
    columns = list(records[0].values)
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=["_candidate_id", *columns])
    writer.writeheader()
    writer.writerows(
        {"_candidate_id": record.row_id, **record.values}
        for record in records
    )
    queries_payload = [
        {
            "query_id": query["query_id"],
            "english_name": query["english_name"],
            "chinese_description": query["chinese_description"],
        }
        for query in queries
    ]
    return [
        {
            "role": "system",
            "content": (
                "For every query, independently select and rank the final semantically related word roots from the complete supplied CSV table. "
                "Use all relevant columns, especially the English name and Chinese description when provided. "
                "If a query has no Chinese description, rely on its English name and the candidate table; do not invent a description. "
                "Never invent candidates or IDs. Return only JSON: "
                "{\"results\":[{\"query_id\":string,\"candidates\":[{\"id\":string,\"score\":number}]}]}. "
                "Include every query_id, even when its candidates list is empty. Scores must be between 0 and 100."
            ),
        },
        {
            "role": "user",
            "content": "Complete CSV table (all ordinary-root records and columns):\n" + csv_buffer.getvalue(),
        },
        {
            "role": "user",
            "content": (
                "Queries:\n" + json.dumps(queries_payload, ensure_ascii=False)
                + f"\nReturn the final top {k} candidates per query, ordered best first. Do not perform a separate reranking stage."
            ),
        },
    ]
async def _invoke_json(
    llm: Any,
    messages: list[dict[str, str]],
    *,
    llm_call_params: dict[str, Any],
) -> dict[str, Any]:
    response = await asyncio.to_thread(llm.invoke, messages, **llm_call_params)
    content = getattr(response, "content", response)
    return _parse_json_object(str(content))


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("word-root candidate model returned invalid JSON")
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("word-root candidate model response must be a JSON object")
    return parsed


def _candidate_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("word-root candidate model response must contain a candidates array")
    return [item for item in candidates if isinstance(item, dict)]


def _matched_result(match_type: str, matches: list[_WordRootRecord]) -> dict[str, Any]:
    return {
        "found": True,
        "match_type": match_type,
        "matches": [record.values for record in matches],
        "candidates": [],
    }


def _validation_skipped_result() -> dict[str, Any]:
    return {
        **_validation_skipped_item_result(),
        "validation_status": "skipped",
        "reason": "word_root_csv_missing",
    }


def _validation_skipped_item_result() -> dict[str, Any]:
    return {
        "found": None,
        "match_type": None,
        "matches": [],
        "candidates": [],
    }


def _normalize_name(value: str) -> str:
    # Preserve case: word-root English names are case-sensitive identifiers.
    return " ".join(value.strip().split())


def _score(value: Any, *, default: float = 0.0) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return default


def _get_cached_result(key: tuple[str, str, str, int, str, str, str]) -> dict[str, Any] | None:
    with _result_cache_lock:
        result = _result_cache.get(key)
        if result is None:
            return None
        _result_cache.move_to_end(key)
        return json.loads(json.dumps(result, ensure_ascii=False))


def _put_cached_result(key: tuple[str, str, str, int, str, str, str], result: dict[str, Any]) -> None:
    with _result_cache_lock:
        _result_cache[key] = json.loads(json.dumps(result, ensure_ascii=False))
        _result_cache.move_to_end(key)
        while len(_result_cache) > _RESULT_CACHE_LIMIT:
            _result_cache.popitem(last=False)
