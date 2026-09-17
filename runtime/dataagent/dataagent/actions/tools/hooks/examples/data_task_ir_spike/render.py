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
"""Deterministic DataTaskIR-to-context renderer; no model is used here."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)
_OMITTED = object()

FIELD_TITLES = {
    "feature_key": "特征主键",
    "statistic_period": "统计周期",
    "fact_filters": "事实表过滤条件",
    "fact_derived_fields": "事实表字段转换",
    "fact_deduplication": "事实表去重规则",
    "dimension_filters": "维度表过滤条件",
    "dimension_derived_fields": "维度表预处理逻辑",
    "dimension_deduplication": "维度表记录身份与版本选择",
    "dimension_relations": "维度表关联条件",
    "time_window": "时间窗口",
    "window_partitioning": "窗口分区键",
    "final_sequence_partitioning": "最终序列分区",
    "final_sequence_ordering": "最终序列排序依据",
    "final_sequence_truncation": "最终序列截断（TopN）",
    "final_sequence_deduplication": "最终序列去重规则",
    "output_fields": "输出字段定义",
    "aggregation_metrics": "聚合指标定义",
    "aggregation_precedence": "去重与聚合的先后关系"
}

_DIMENSION_SCOPE_FIELDS = ("dimension_filters", "dimension_derived_fields", "dimension_deduplication")


def render_context(field_outputs: list[dict[str, Any]]) -> str:
    """Render only confirmed DataTaskIR leaves into stable NL2SQL query context."""
    by_id = {str(output.get("field_id", "")): output for output in field_outputs}
    dimension_sources = _confirmed_dimension_sources(by_id.get("dimension_relations"))
    lines = [
        "【DataTaskIR 口径约束】",
        "以下仅包含已确认口径；未出现的内容不构成约束。",
    ]
    warnings: list[str] = []
    for field_id in FIELD_TITLES:
        output = by_id.get(field_id)
        if output is None:
            continue
        unresolved = _normalize_unresolved(output.get("unresolved"))
        question = output.get("question")
        raw_value = output.get("value", {})
        value_source = raw_value if isinstance(raw_value, dict) else {}
        omitted_paths: list[str] = []
        pruned = _prune_value(value_source, "$", bool(unresolved), omitted_paths)
        value = pruned if isinstance(pruned, dict) else {}
        if field_id in _DIMENSION_SCOPE_FIELDS and value:
            value = _filter_dimension_scopes(value, dimension_sources)
        _log_omissions(field_id, unresolved, question, omitted_paths)
        if unresolved:
            question_text = question if isinstance(question, str) and question.strip() else ""
            warning = f"{FIELD_TITLES.get(field_id)}：存在未确认口径"
            if question_text:
                warning += f"（待澄清问题：{question_text}）"
            # 当前warning未加入到IR渲染内容中
            warnings.append(warning)
        if not value:
            continue
        renderer = _RENDERERS.get(field_id, _render_json_value)
        value_text = renderer(value)
        if value_text:
            lines.append(f"- {FIELD_TITLES.get(field_id)}：{value_text}")
    if not _has_confirmed_dedup(by_id) and any(by_id.get(field_id) for field_id in ("fact_deduplication", "dimension_deduplication", "final_sequence_deduplication")):
        lines.append(
            "- 去重约束：本任务未确认任何去重要求（事实表去重、维度表去重、最终序列去重均无已确认内容）。"
            "用户未明确要求去重时，禁止对事实记录或最终输出执行去重；源表为天级增量表或存在多分区不作为去重依据。"
            "维表在 JOIN 前为保证关联键唯一所必需的去重按 JOIN 安全规则执行，不在此限。"
        )
    return "\n".join(lines)


def _has_confirmed_dedup(by_id: dict[str, dict[str, Any]]) -> bool:
    """Whether any dedup dimension (fact/dimension/final sequence) recorded confirmed content."""
    fact = by_id.get("fact_deduplication")
    if fact:
        value = fact.get("value", {})
        if isinstance(value, dict):
            identity = value.get("record_identity", {}) or {}
            selection = value.get("record_selection", {}) or {}
            if identity.get("fields") or selection.get("criteria"):
                return True
    dimension = by_id.get("dimension_deduplication")
    if dimension:
        value = dimension.get("value", {})
        if isinstance(value, dict) and value.get("scopes"):
            return True
    final = by_id.get("final_sequence_deduplication")
    if final:
        value = final.get("value", {})
        if isinstance(value, dict) and value.get("on_duplicate") is not None:
            return True
    return False


def _confirmed_dimension_sources(output: dict[str, Any] | None) -> set[str]:
    """Collect dimension sources confirmed by dimension_relations (raw, unpruned)."""
    if output is None:
        return set()
    raw = output.get("value", {})
    value = raw if isinstance(raw, dict) else {}
    relations = value.get("relations", [])
    if not isinstance(relations, list):
        return set()
    return {
        str(relation.get("dimension_source_ref"))
        for relation in relations
        if isinstance(relation, dict) and relation.get("dimension_source_ref")
    }


def _filter_dimension_scopes(value: dict[str, Any], dimension_sources: set[str]) -> dict[str, Any]:
    """Keep only scopes whose source_ref is confirmed as a dimension source."""
    scopes = value.get("scopes", [])
    if not isinstance(scopes, list):
        return {"scopes": []}
    return {
        "scopes": [
            scope
            for scope in scopes
            if isinstance(scope, dict) and scope.get("source_ref") in dimension_sources
        ]
    }


def _normalize_unresolved(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _prune_value(value: Any, path: str, drop_empty: bool, omitted_paths: list[str]) -> Any:
    if value is None:
        omitted_paths.append(path)
        return _OMITTED
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            pruned = _prune_value(child, f"{path}.{key}", drop_empty, omitted_paths)
            if pruned is not _OMITTED:
                result[key] = pruned
        if not result and drop_empty:
            omitted_paths.append(path)
            return _OMITTED
        return result
    if isinstance(value, list):
        result = []
        for index, child in enumerate(value):
            pruned = _prune_value(child, f"{path}[{index}]", drop_empty, omitted_paths)
            if pruned is not _OMITTED:
                result.append(pruned)
        if not result and drop_empty:
            omitted_paths.append(path)
            return _OMITTED
        return result
    return value


def _log_omissions(
    field_id: str,
    unresolved: list[str],
    question: Any,
    omitted_paths: list[str],
) -> None:
    details = {
        "field_id": field_id,
        "unresolved": unresolved,
        "question": question if isinstance(question, str) and question.strip() else None,
        "omitted_paths": sorted(set(omitted_paths)),
    }
    rendered = json.dumps(details, ensure_ascii=False, sort_keys=True)
    if unresolved:
        logger.warning("DataTaskIR context omitted unresolved values: %s", rendered)
    elif omitted_paths:
        logger.debug("DataTaskIR context omitted None values: %s", rendered)


def _attributes(items: list[tuple[str, Any]]) -> str:
    return "，".join(f"{label}={value}" for label, value in items if value is not None)


def _render_feature_key(value: dict[str, Any]) -> str:
    parts = []
    for item in value.get("components", []):
        attributes = _attributes(
            [
                ("角色", item.get("semantic_role")),
                ("空值语义", item.get("null_semantics")),
            ]
        )
        field_ref = item.get("field_ref")
        if field_ref is not None:
            parts.append(f"{field_ref}（{attributes}）" if attributes else str(field_ref))
    rendered = []
    if parts:
        rendered.append(f"由 {' + '.join(parts)} 联合构成")
    if value.get("uniqueness_scope") is not None:
        rendered.append(f"唯一范围={value.get('uniqueness_scope')}")
    return "；".join(rendered) + ("。" if rendered else "")


def _render_statistic_period(value: dict[str, Any]) -> str:
    rendered = []
    if value.get("time_field") is not None:
        rendered.append(f"按 {value.get('time_field')} 分桶")
    attributes = _attributes(
        [
            ("粒度", value.get("grain")),
            ("时区", value.get("timezone")),
            ("日历", value.get("calendar")),
            ("周起始", value.get("week_start")),
            ("自定义定义", value.get("custom_definition")),
        ]
    )
    if attributes:
        rendered.append(attributes)
    return "；".join(rendered) + ("。" if rendered else "")


def _render_filter_scope(item: dict[str, Any]) -> str | None:
    """Render the application scope of a filter predicate, or None when unset."""
    filter_scope = item.get("filter_scope")
    if filter_scope == "window_branch":
        branch = item.get("branch_ref")
        return f"仅{branch}分支" if branch else "仅所属窗口分支"
    if filter_scope == "source_level":
        return "整个事实源"
    return None


def _render_predicate(item: dict[str, Any], source_ref: Any) -> str:
    source = f"{source_ref}." if source_ref is not None else ""
    base = " ".join(
        str(value)
        for value in [
            f"{source}{item.get('field_ref')}" if item.get("field_ref") is not None else None,
            item.get("operator"),
        ]
        if value is not None
    )
    if "operands" in item:
        operands = json.dumps(item.get("operands"), ensure_ascii=False, separators=(",", ":"))
        base = f"{base} {operands}".strip()
    attributes = [
        ("常量类型", item.get("operand_type")),
        ("空值语义", item.get("null_semantics")),
    ]
    scope = _render_filter_scope(item)
    if scope is not None:
        attributes.append(("作用范围", scope))
    rendered_attributes = _attributes(attributes)
    return f"{base}（{rendered_attributes}）" if rendered_attributes else base


def _render_fact_filters(value: dict[str, Any]) -> str:
    predicates = value.get("predicates", [])
    if not predicates:
        return "不需要额外事实过滤。"
    rendered = [_render_predicate(item, item.get("source_ref")) for item in predicates]
    text = "且".join(item for item in rendered if item)
    if any(item.get("filter_scope") == "window_branch" for item in predicates):
        text += "。窗口分支过滤仅作用于对应分支，禁止提升为对整个事实源的全局过滤；合并各窗口结果时必须以全部窗口用户的并集为基准，禁止以单个窗口用户集合为基准"
    return text + "。"


def _render_derived(item: dict[str, Any], source_ref: Any) -> str:
    source = f"{source_ref}." if source_ref is not None else ""
    target = item.get("target_field")
    expression = item.get("expression")
    base = ""
    if target is not None and expression is not None:
        base = f"{source}{target} := {expression}"
    elif target is not None:
        base = f"目标字段={source}{target}"
    elif expression is not None:
        base = f"转换表达式={expression}"
    attributes = _attributes(
        [
            ("输入", item.get("input_fields")),
            ("输出类型", item.get("output_type")),
            ("单位", item.get("unit")),
            ("舍入", item.get("rounding")),
            ("空值语义", item.get("null_semantics")),
        ]
    )
    return f"{base}（{attributes}）" if base and attributes else base or attributes


def _render_fact_derived(value: dict[str, Any]) -> str:
    items = value.get("derived_fields", [])
    if not items:
        return "不需要事实字段转换。"
    rendered = [_render_derived(item, item.get("source_ref")) for item in items]
    return "；".join(item for item in rendered if item) + "。"


def _render_selection(selection: dict[str, Any]) -> str:
    rendered = []
    criteria = selection.get("criteria", [])
    if criteria:
        parts = []
        for item in criteria:
            base = _attributes([("字段", item.get("field_ref")), ("选择", item.get("choose"))])
            attributes = _attributes(
                [
                    ("类型", item.get("value_type")),
                    ("空值位置", item.get("null_rank")),
                ]
            )
            parts.append(f"{base}（{attributes}）" if attributes else base)
        rendered.append("，再".join(item for item in parts if item))
    elif "criteria" in selection:
        rendered.append("无选择条件")
    if selection.get("if_all_criteria_equal") is not None:
        rendered.append(f"全部条件相同时={selection.get('if_all_criteria_equal')}")
    return "；".join(rendered)


def _render_fact_dedup(value: dict[str, Any]) -> str:
    identity = value.get("record_identity", {})
    selection = value.get("record_selection", {})
    identity_fields = identity.get("fields", []) or []
    if not identity_fields and not selection.get("criteria"):
        return "不需要事实记录去重。"
    rendered = []
    identity_text = _attributes(
        [
            ("同一事实字段", identity_fields),
            ("空值等价", identity.get("null_equality")),
        ]
    )
    if identity_text:
        rendered.append(identity_text)
    if identity_fields and not selection.get("criteria") and selection.get("if_all_criteria_equal") is None:
        rendered.append("未定义选择函数，不产生去重")
    else:
        selection_text = _render_selection(selection)
        if selection_text:
            rendered.append(selection_text)
    return "；".join(rendered) + "。"


def _render_dimension_filters(value: dict[str, Any]) -> str:
    scopes = value.get("scopes", [])
    if not scopes:
        return "不需要额外维度过滤。"
    rendered = []
    for scope in scopes:
        source = scope.get("source_ref")
        predicates = [_render_predicate(item, source) for item in scope.get("predicates", [])]
        rendered.append("且".join(item for item in predicates if item))
    return "；".join(item for item in rendered if item) + "。"


def _render_dimension_derived(value: dict[str, Any]) -> str:
    scopes = value.get("scopes", [])
    if not scopes:
        return "不需要维度字段预处理。"
    rendered = []
    for scope in scopes:
        source = scope.get("source_ref")
        rendered.extend(_render_derived(item, source) for item in scope.get("derived_fields", []))
    return "；".join(item for item in rendered if item) + "。"


def _render_dimension_dedup(value: dict[str, Any]) -> str:
    scopes = value.get("scopes", [])
    if not scopes:
        return "不需要维度去重或版本选择。"
    rendered = []
    for scope in scopes:
        identity = scope.get("record_identity", {})
        parts = []
        if scope.get("source_ref") is not None:
            parts.append(str(scope.get("source_ref")))
        identity_text = _attributes(
            [
                ("同一实体字段", identity.get("fields")),
                ("空值等价", identity.get("null_equality")),
            ]
        )
        if identity_text:
            parts.append(identity_text)
        selection_text = _render_selection(scope.get("record_selection", {}))
        if selection_text:
            parts.append(selection_text)
        rendered.append("：".join(parts[:2]) + ("；" + "；".join(parts[2:]) if len(parts) > 2 else ""))
    return "；".join(item for item in rendered if item) + "。"


def _render_relations(value: dict[str, Any]) -> str:
    relations = value.get("relations", [])
    if not relations:
        return "不需要维度关联。"
    rendered = []
    for relation in relations:
        parts = []
        sources = [
            relation.get("fact_source_ref"),
            relation.get("join_type"),
            "join",
            relation.get("dimension_source_ref"),
        ]
        source_text = " ".join(str(item) for item in sources if item is not None)
        if source_text:
            parts.append(source_text)
        conditions = []
        for item in relation.get("conditions", []):
            base = " ".join(
                str(child)
                for child in [item.get("fact_field"), item.get("operator"), item.get("dimension_field")]
                if child is not None
            )
            attributes = _attributes([("null匹配", item.get("null_matches"))])
            conditions.append(f"{base}（{attributes}）" if attributes else base)
        if conditions:
            parts.append(f"on {' 且 '.join(item for item in conditions if item)}")
        attributes = _attributes(
            [
                ("基数", relation.get("cardinality")),
                ("未匹配事实", relation.get("unmatched_fact")),
                ("时间有效条件", relation.get("temporal_validity")),
            ]
        )
        if attributes:
            parts.append(attributes)
        rendered.append("；".join(parts))
    return "；".join(item for item in rendered if item) + "。"


def _render_time_window(value: dict[str, Any]) -> str:
    rendered = []
    if value.get("time_field") is not None:
        rendered.append(f"使用 {value.get('time_field')} 判定")
    anchor = value.get("anchor", {})
    anchor_text = _attributes(
        [
            ("锚点字段", anchor.get("field")),
            ("锚点值", anchor.get("value")),
        ]
    )
    if anchor_text:
        rendered.append(anchor_text)
    range_value = value.get("range", {})
    range_parts = [range_value.get("direction"), range_value.get("number"), range_value.get("unit")]
    range_text = " ".join(str(item) for item in range_parts if item is not None)
    if range_text:
        rendered.append(f"范围={range_text}")
    range_attributes = _attributes(
        [
            ("范围类型", range_value.get("type")),
            ("包含锚点周期", range_value.get("include_anchor_period")),
        ]
    )
    if range_attributes:
        rendered.append(range_attributes)
    boundary = value.get("boundary", {})
    boundary_text = _attributes(
        [
            ("起点包含", boundary.get("start_inclusive")),
            ("终点包含", boundary.get("end_inclusive")),
        ]
    )
    if boundary_text:
        rendered.append(boundary_text)
    if value.get("null_time_semantics") is not None:
        rendered.append(f"空时间={value.get('null_time_semantics')}")
    return "；".join(rendered) + ("。" if rendered else "")


def _render_window_partitioning(value: dict[str, Any]) -> str:
    """Render window function PARTITION BY key, distinct from final sequence partitioning."""
    fields = value.get("partition_fields", [])
    if not fields:
        return "不使用窗口函数，无需窗口分区键。"
    rendered = [f"窗口函数分区键={fields}"]
    if value.get("null_equality") is not None:
        rendered.append(f"空值等价={value.get('null_equality')}")
    return "；".join(rendered) + "。"


def _render_partitioning(value: dict[str, Any]) -> str:
    fields = value.get("partition_fields", [])
    if not fields:
        return "不需要逻辑序列分区。"
    rendered = [f"由 {fields} 定义同一逻辑序列"]
    if value.get("null_equality") is not None:
        rendered.append(f"空值等价={value.get('null_equality')}")
    return "；".join(rendered) + "。"


def _render_ordering(value: dict[str, Any]) -> str:
    criteria = value.get("criteria", [])
    if not criteria:
        return "不需要最终序列排序。"
    parts = []
    for item in criteria:
        base = " ".join(str(child) for child in [item.get("field_ref"), item.get("direction")] if child is not None)
        attributes = _attributes(
            [
                ("类型", item.get("value_type")),
                ("空值位置", item.get("null_rank")),
            ]
        )
        parts.append(f"{base}（{attributes}）" if attributes else base)
    rendered = [f"按 {'，再按 '.join(item for item in parts if item)}"]
    if value.get("tie_behavior") is not None:
        rendered.append(f"全部相同时={value.get('tie_behavior')}")
    return "；".join(rendered) + "。"


def _render_final_dedup(value: dict[str, Any]) -> str:
    if value.get("on_duplicate") is None:
        return ""
    identity = value.get("identity_ref")
    if identity == "custom":
        identity = f"custom:{value.get('custom_identity_fields', [])}"
    rendered = []
    if identity is not None:
        rendered.append(f"重复身份={identity}")
    rendered.append(f"重复行为={value.get('on_duplicate')}")
    selection = _render_selection(value.get("record_selection", {}))
    if selection:
        rendered.append(selection)
    return "；".join(rendered) + "。"


def _render_final_truncation(value: dict[str, Any]) -> str:
    limit = value.get("limit_per_partition")
    if limit is None:
        return ""
    tie_behavior = value.get("tie_behavior")
    tie_text = ""
    if tie_behavior == "retain_all":
        tie_text = "；并列时全部保留"
    elif tie_behavior == "truncate_exact":
        tie_text = "；并列时仍只取上限条数"
    return f"每个逻辑序列保留前 {limit} 条{tie_text}。"


def _render_json_value(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _render_output_fields(value: dict[str, Any]) -> str:
    fields = value.get("fields", [])
    if not fields:
        return "无已确认输出字段。"
    rendered = []
    for item in fields:
        expression = item.get("expression")
        source = item.get("source")
        if expression:
            origin_text = f"计算口径={expression}"
            if source:
                origin_text += f"（来源={source}）"
        else:
            origin_text = f"来源={source or '无来源'}"
        rendered.append(
            f"{item.get('field_name')}: {origin_text}，类型={item.get('output_type')}，"
            f"说明={item.get('description')}"
        )
    return "；".join(rendered) + "。"


def _render_aggregation_metrics(value: dict[str, Any]) -> str:
    count_metrics = value.get("count_metrics", [])
    ratio_metrics = value.get("ratio_metrics", [])
    avg_metrics = value.get("avg_metrics", [])
    parts = []
    for item in count_metrics:
        count_type = item.get("count_type", "未明确")
        count_entity = item.get("count_entity")
        group_by = item.get("group_by", [])
        entity_text = f"，计数实体={count_entity}" if count_entity else ""
        parts.append(
            f"{item.get('metric_name')}: 计数类型={count_type}{entity_text}，分组键={group_by}"
        )
    for item in ratio_metrics:
        parts.append(
            f"{item.get('metric_name')}: 分子={item.get('numerator')}，分母={item.get('denominator')}，"
            f"精度={item.get('precision')}"
        )
    for item in avg_metrics:
        parts.append(
            f"{item.get('metric_name')}: 分子={item.get('numerator')}，分母={item.get('denominator')}，"
            f"分组键={item.get('group_by')}"
        )
    return "；".join(parts) if parts else "无已确认聚合指标。"


def _render_aggregation_precedence(value: dict[str, Any]) -> str:
    dedup_before = value.get("deduplication_before_aggregation")
    aggregation_key = value.get("aggregation_key", [])
    count_semantics = value.get("count_semantics")
    input_grain = value.get("input_grain")
    parts = []
    if aggregation_key:
        dedup_text = "去重先于聚合" if dedup_before else "聚合基于原始记录（不去重）"
        parts.append(dedup_text)
        parts.append(f"聚合分组键={aggregation_key}")
        if count_semantics:
            parts.append(f"计数语义={count_semantics}")
    else:
        parts.append("本任务无聚合计算（无分组键），禁止自行添加聚合或按其他粒度分组")
    if input_grain:
        parts.append(f"聚合输入数据粒度={input_grain}")
    return "；".join(parts) + "。" if parts else ""


_RENDERERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "feature_key": _render_feature_key,
    "statistic_period": _render_statistic_period,
    "fact_filters": _render_fact_filters,
    "fact_derived_fields": _render_fact_derived,
    "fact_deduplication": _render_fact_dedup,
    "dimension_filters": _render_dimension_filters,
    "dimension_derived_fields": _render_dimension_derived,
    "dimension_deduplication": _render_dimension_dedup,
    "dimension_relations": _render_relations,
    "time_window": _render_time_window,
    "window_partitioning": _render_window_partitioning,
    "final_sequence_partitioning": _render_partitioning,
    "final_sequence_ordering": _render_ordering,
    "final_sequence_truncation": _render_final_truncation,
    "final_sequence_deduplication": _render_final_dedup,
    "output_fields": _render_output_fields,
    "aggregation_metrics": _render_aggregation_metrics,
    "aggregation_precedence": _render_aggregation_precedence,
}
