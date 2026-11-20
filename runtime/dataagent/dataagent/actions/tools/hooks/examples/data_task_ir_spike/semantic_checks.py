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
"""Deterministic DataTaskIR semantic warnings (join≠identity, list_agg, hour pt_h)."""

from __future__ import annotations

import re
from typing import Any, Iterable

_AGG_KINDS = frozenset(
    {
        "passthrough",
        "count",
        "sum",
        "avg",
        "distinct_count",
        "list_agg",
        "expression",
        None,
        "null",
    }
)
_LIST_AGG_PATTERN = re.compile(
    r"collect_list|collect_set|array_agg|list_agg|concat_ws\s*\(.*collect",
    re.IGNORECASE,
)
_LIST_AGG_EVIDENCE = re.compile(
    r"拼接|序列|collect[_ ]?(?:list|set)|逗号连接|多行拼|list_agg|排成序列",
    re.IGNORECASE,
)
_SEARCH_JOIN_KEYS = frozenset(
    {"query", "app_cn_name", "search_keyword", "search_words", "search_word"}
)
# Subject type → query hints + canonical entity keys for identity (not JOIN keys).
_ENTITY_SUBJECT_SPECS: tuple[dict[str, Any], ...] = (
    {
        "kind": "app",
        "label": "应用",
        "phrase": "为各应用/按应用",
        "keys": ("app_id", "appid"),
        "hints": (
            "各应用",
            "每个应用",
            "按应用",
            "应用级",
            "为各应用",
            "应用id",
            "app_id",
        ),
    },
    {
        "kind": "device",
        "label": "设备",
        "phrase": "为各设备/按设备",
        "keys": ("device_id", "device_id2", "did", "dsid"),
        "hints": (
            "每个设备",
            "各设备",
            "按设备",
            "设备级",
            "为各设备",
            "device_id",
        ),
    },
)
# Hour-partitioned fact tables commonly use _hm suffix or explicit pt_h.
_HOUR_TABLE_RE = re.compile(r"(?:_hm|_hh)(?:\b|$)|(?:^|[.\s])pt_h(?:\b|$)", re.IGNORECASE)


def collect_ir_semantic_warnings(
    fields: list[dict[str, Any]],
    *,
    user_query: str = "",
) -> list[str]:
    """Return human-readable warnings; empty means no deterministic issue found."""
    by_id = {
        str(field.get("field_id")): field
        for field in fields
        if isinstance(field, dict) and field.get("field_id")
    }
    warnings: list[str] = []
    warnings.extend(_warn_join_key_as_identity(by_id, user_query or ""))
    warnings.extend(_warn_list_agg_without_evidence(by_id, user_query or ""))
    warnings.extend(_warn_invalid_aggregation_kind(by_id))
    warnings.extend(_warn_hour_partition_incomplete(by_id, user_query or ""))
    return warnings


def _field_value(by_id: dict[str, dict[str, Any]], field_id: str) -> dict[str, Any]:
    field = by_id.get(field_id) or {}
    value = field.get("value")
    return value if isinstance(value, dict) else {}


def _identity_refs(by_id: dict[str, dict[str, Any]]) -> set[str]:
    value = _field_value(by_id, "identity")
    refs: set[str] = set()
    for item in value.get("components") or []:
        if isinstance(item, dict) and item.get("field_ref"):
            refs.add(str(item["field_ref"]).strip())
    return {ref for ref in refs if ref}


def _join_condition_fields(by_id: dict[str, dict[str, Any]]) -> set[str]:
    value = _field_value(by_id, "sources")
    refs: set[str] = set()
    for dim in value.get("dimensions") or []:
        if not isinstance(dim, dict):
            continue
        join = dim.get("join")
        if not isinstance(join, dict):
            continue
        for cond in join.get("conditions") or []:
            if not isinstance(cond, dict):
                continue
            for key in ("fact_field", "dim_field"):
                if cond.get(key):
                    refs.add(str(cond[key]).strip())
    return {ref for ref in refs if ref}


def _match_entity_subject_specs(user_query: str) -> list[dict[str, Any]]:
    query_l = (user_query or "").lower()
    return [
        spec
        for spec in _ENTITY_SUBJECT_SPECS
        if any(str(hint).lower() in query_l for hint in (spec.get("hints") or ()))
    ]


def _all_entity_subject_keys() -> set[str]:
    keys: set[str] = set()
    for spec in _ENTITY_SUBJECT_SPECS:
        keys.update(str(k).lower() for k in (spec.get("keys") or ()))
    return keys


def _warn_join_key_as_identity(
    by_id: dict[str, dict[str, Any]],
    user_query: str,
) -> list[str]:
    identity = _identity_refs(by_id)
    join_fields = _join_condition_fields(by_id)
    if not identity or not join_fields:
        return []
    search_identity = {ref for ref in identity if ref.lower() in _SEARCH_JOIN_KEYS}
    if not search_identity:
        return []
    join_lower = {j.lower() for j in join_fields}
    if not any(ref.lower() in join_lower for ref in search_identity):
        return []

    identity_lower = {ref.lower() for ref in identity}
    matched = _match_entity_subject_specs(user_query)
    if matched:
        expected_keys = {
            str(k).lower() for spec in matched for k in (spec.get("keys") or ())
        }
        if any(k in identity_lower for k in expected_keys):
            # Entity subject key already present → search JOIN keys in identity are
            # still suboptimal, but do not push a wrong replacement key.
            return []
        # Prefer a single primary recommendation when multiple specs match.
        primary = matched[0]
        key_txt = "/".join(str(k) for k in (primary.get("keys") or ()))
        phrase = str(primary.get("phrase") or primary.get("label") or "任务主体")
        label = str(primary.get("label") or "主体")
        return [
            "identity/sources：JOIN 关联键（如 query/app_cn_name）被写进 identity。"
            f"关联键默认不是输出主键；当任务是「{phrase}」时，identity 应使用{label}实体键"
            f"（通常 {key_txt}），当前 identity={sorted(identity)}。"
            "请把 query/app_cn_name 留在 join.conditions，修正 identity.components。"
        ]

    # No subject hint: only warn when no known entity key is present either.
    if any(k in identity_lower for k in _all_entity_subject_keys()):
        return []
    return [
        "identity/sources：JOIN 关联键（如 query/app_cn_name）被写进 identity。"
        "关联键默认不是输出主键；identity 应使用任务统计主体实体键"
        "（应用→app_id，设备→device_id 等），"
        f"当前 identity={sorted(identity)}。"
        "请把 query/app_cn_name 留在 join.conditions，修正 identity.components。"
    ]


def _iter_measure_like(value: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for key in ("measures", "features", "extras"):
        for item in value.get(key) or []:
            if isinstance(item, dict):
                label = item.get("measure_id") or item.get("field_name") or key
                yield str(label), item


def _warn_list_agg_without_evidence(
    by_id: dict[str, dict[str, Any]],
    user_query: str,
) -> list[str]:
    value = _field_value(by_id, "output_features")
    if not value:
        return []
    has_evidence = bool(_LIST_AGG_EVIDENCE.search(user_query or ""))
    warnings: list[str] = []
    for label, item in _iter_measure_like(value):
        aggregation = item.get("aggregation") if isinstance(item.get("aggregation"), dict) else {}
        calc = item.get("calc") if isinstance(item.get("calc"), dict) else {}
        kind = aggregation.get("kind")
        blob = " ".join(
            str(part)
            for part in (
                kind,
                calc.get("intent"),
                aggregation.get("expression_template"),
                item.get("description"),
            )
            if part
        )
        mentions_collect = bool(_LIST_AGG_PATTERN.search(blob))
        if kind == "list_agg" or mentions_collect:
            if has_evidence and kind == "list_agg":
                continue
            if kind == "list_agg" and not has_evidence:
                warnings.append(
                    f"output_features：指标/列 {label} 的 aggregation.kind=list_agg，"
                    "但用户原话与证据未要求拼接/序列/collect；请改为 passthrough（透传已有列）"
                    "或补充拼接证据后再用 list_agg。"
                )
            elif mentions_collect and kind != "list_agg":
                warnings.append(
                    f"output_features：指标/列 {label} 的 calc.intent/expression 出现 collect_list"
                    "等拼接语义，但 aggregation.kind 不是 list_agg。"
                    "若只需透传已有字符串列，改用 kind=passthrough 并删除 collect 表述；"
                    "若确需多行拼接，kind 必须为 list_agg 且用户有拼接证据。"
                )
    return warnings


def _warn_invalid_aggregation_kind(by_id: dict[str, dict[str, Any]]) -> list[str]:
    value = _field_value(by_id, "output_features")
    warnings: list[str] = []
    for label, item in _iter_measure_like(value):
        aggregation = item.get("aggregation")
        if not isinstance(aggregation, dict):
            continue
        kind = aggregation.get("kind")
        if kind in _AGG_KINDS:
            continue
        warnings.append(
            f"output_features：指标/列 {label} 的 aggregation.kind={kind!r} 非法；"
            "允许值为 passthrough|count|sum|avg|distinct_count|list_agg|expression|null。"
        )
    return warnings


def _iter_source_entries(by_id: dict[str, dict[str, Any]]) -> Iterable[dict[str, Any]]:
    sources = _field_value(by_id, "sources")
    for bucket in (sources.get("facts") or [], sources.get("dimensions") or []):
        for entry in bucket:
            if isinstance(entry, dict):
                yield entry


def _source_refs(by_id: dict[str, dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for entry in _iter_source_entries(by_id):
        ref = entry.get("source_ref")
        if ref:
            refs.append(str(ref))
    return refs


def _filter_field_refs(by_id: dict[str, dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for entry in _iter_source_entries(by_id):
        for pred in entry.get("filters") or []:
            if isinstance(pred, dict) and pred.get("field_ref"):
                refs.add(str(pred["field_ref"]).strip().lower())
    return refs


def _looks_hour_partitioned(by_id: dict[str, dict[str, Any]], user_query: str) -> bool:
    if "pt_h" in (user_query or "").lower() or "小时分区" in (user_query or ""):
        return True
    if "pt_h" in _filter_field_refs(by_id):
        return True
    for ref in _source_refs(by_id):
        if _HOUR_TABLE_RE.search(ref):
            return True
        # bare table token ending with _hm / _hh
        token = ref.rsplit(".", 1)[-1].lower()
        if token.endswith("_hm") or token.endswith("_hh"):
            return True
    return False


_ALL_DAY_HOUR_MARKERS = (
    "当天全部小时",
    "全部小时聚合",
    "全天小时",
    "所有小时合",
    "当天所有小时",
    "不按 pt_h",
    "不要指定小时",
)


def _time_blob(by_id: dict[str, dict[str, Any]]) -> str:
    field = by_id.get("time_context") or {}
    value = _field_value(by_id, "time_context")
    bucket = value.get("output_bucket") if isinstance(value.get("output_bucket"), dict) else {}
    sources_field = by_id.get("sources") or {}
    return " ".join(
        str(part)
        for part in (
            bucket.get("custom_definition"),
            field.get("rationale"),
            sources_field.get("rationale"),
        )
        if part
    )


def _time_declares_single_hour(by_id: dict[str, dict[str, Any]]) -> bool:
    value = _field_value(by_id, "time_context")
    bucket = value.get("output_bucket") if isinstance(value.get("output_bucket"), dict) else {}
    grain = str(bucket.get("grain") or "").lower()
    time_field = str(bucket.get("time_field") or "").lower()
    custom = str(bucket.get("custom_definition") or "")
    if grain == "hour" or time_field == "pt_h":
        return True
    if "pt_h" in custom.lower() and not any(m in custom for m in _ALL_DAY_HOUR_MARKERS):
        return True
    return False


def _all_day_hours_confirmed(by_id: dict[str, dict[str, Any]]) -> bool:
    """True when IR explicitly records all-hours-of-day aggregation (no pt_h slice)."""
    blob = _time_blob(by_id)
    if any(m in blob for m in _ALL_DAY_HOUR_MARKERS):
        return True
    return False


def _hour_clarification_pending(by_id: dict[str, dict[str, Any]]) -> bool:
    field = by_id.get("time_context") or {}
    unresolved = field.get("unresolved") or []
    question = str(field.get("question") or "")
    blob = " ".join([*(str(u) for u in unresolved), question])
    return any(k in blob for k in ("小时", "pt_h", "单小时", "全部小时"))


def _warn_hour_partition_incomplete(
    by_id: dict[str, dict[str, Any]],
    user_query: str,
) -> list[str]:
    if not _looks_hour_partitioned(by_id, user_query):
        return []
    if _hour_clarification_pending(by_id):
        return []
    has_pt_h = "pt_h" in _filter_field_refs(by_id)
    has_pt_d = "pt_d" in _filter_field_refs(by_id)
    single_hour = _time_declares_single_hour(by_id)
    all_day = _all_day_hours_confirmed(by_id)

    # Confirmed all-day: pt_d only, no pt_h — valid.
    if all_day and has_pt_d and not has_pt_h and not single_hour:
        return []
    # Confirmed single-hour: need pt_h (+ usually pt_d).
    if has_pt_h and (single_hour or has_pt_d) and not all_day:
        return []
    # Conflict: all-day claim vs single-hour grain/time_field (either direction).
    if all_day and single_hour:
        return [
            "sources/time_context：全天与单小时口径自相矛盾——"
            "已声明「当天全部小时聚合」，但 time_context 仍写 grain=hour / time_field=pt_h；"
            "请二选一：全天则 grain=day、time_field=pt_d、sources 只留 pt_d；"
            "单小时则补齐 pt_d+pt_h 并去掉全天声明。"
        ]
    # Conflict: claimed all-day but missing day filter.
    if all_day and not has_pt_d:
        return [
            "sources/time_context：已确认当天全部小时聚合，但 sources 未按 pt_d 过滤；"
            "全天小时口径须写 pt_d eq [\"$date\"]，且不要写 pt_h。"
        ]
    # Conflict: claimed all-day but still filters pt_h.
    if all_day and has_pt_h:
        return [
            "sources/time_context：已声明当天全部小时聚合，但 sources 仍过滤 pt_h；"
            "全天小时口径应只保留 pt_d，写入也仅天分区。"
        ]

    missing: list[str] = []
    if not has_pt_h and not all_day:
        missing.append(
            "未确认单小时（应有 pt_h eq [\"$hour\"]）也未明示「当天全部小时聚合」"
        )
    if not single_hour and not all_day and not has_pt_h:
        missing.append("time_context 未声明 grain=hour/pt_h，也未写明全天小时聚合")
    if has_pt_d and not has_pt_h and not all_day:
        missing.append("已有 pt_d 却无 pt_h，且未标明当天全部小时聚合")
    if not missing:
        return []
    return [
        "sources/time_context：源表像小时分区表，但小时口径不完整——"
        + "；".join(missing)
        + "。请补齐单小时 pt_d+pt_h，或明示「当天全部小时聚合」并只按 pt_d 过滤/写入。"
    ]
