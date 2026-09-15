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
"""Deterministic 100-case benchmark generator for the DataTaskIR spike."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

DOMAINS = [
    ("retail", "customer_id", "客户", "order_time", "orders", "dim_customer"),
    ("payment", "account_id", "账户", "payment_time", "payments", "dim_account"),
    ("advertising", "campaign_id", "广告计划", "exposure_time", "ad_events", "dim_campaign"),
    ("logistics", "parcel_id", "包裹", "scan_time", "parcel_events", "dim_parcel"),
    ("support", "ticket_id", "工单", "action_time", "ticket_events", "dim_ticket"),
    ("streaming", "viewer_id", "观众", "play_time", "play_events", "dim_viewer"),
    ("lending", "loan_id", "贷款", "transaction_time", "loan_events", "dim_loan"),
    ("healthcare", "patient_id", "患者", "visit_time", "visit_events", "dim_patient"),
    ("gaming", "player_id", "玩家", "event_time", "game_events", "dim_player"),
    ("energy", "meter_id", "电表", "reading_time", "meter_readings", "dim_meter"),
]

FIELD_EVIDENCE_LINE_INDEXES = {
    "feature_key": [0, 13],
    "statistic_period": [1],
    "fact_filters": [2],
    "fact_derived_fields": [3],
    "fact_deduplication": [4],
    "dimension_filters": [5],
    "dimension_derived_fields": [6],
    "dimension_deduplication": [7],
    "dimension_relations": [8],
    "time_window": [9],
    "final_sequence_partitioning": [10, 13],
    "final_sequence_ordering": [11],
    "final_sequence_deduplication": [12],
}


def generate_benchmark_cases(count: int = 100) -> list[dict[str, Any]]:
    """Generate reproducible complete, optional, incomplete, and conflicting benchmark cases."""
    if count < 1 or count > 100:
        raise ValueError("count must be between 1 and 100")
    return [_build_case(index) for index in range(count)]


def _build_case(index: int) -> dict[str, Any]:
    domain, entity_key, role, time_field, fact_source, dimension_source = DOMAINS[index % len(DOMAINS)]
    values = _base_values(entity_key, role, time_field, fact_source, dimension_source)
    lines = _base_evidence_lines(entity_key, role, time_field, fact_source, dimension_source)
    expect_unresolved = set()
    expect_not_applicable = set()
    mode = "complete"
    if 60 <= index < 75:
        mode = "optional"
        _apply_optional_variant(index, values, lines, expect_not_applicable)
    elif 75 <= index < 90:
        mode = "incomplete"
        _apply_incomplete_variant(index, values, lines, expect_unresolved)
    elif index >= 90:
        mode = "conflict"
        _apply_conflict_variant(index, values, lines, expect_unresolved)
    user_query = f"请生成 {domain} 场景的日级特征序列，严格使用工具给出的数据口径。"
    evidence_style = index % 4
    return {
        "case_id": f"case_{index + 1:03d}",
        "mode": mode,
        "user_query": user_query,
        "confirmed_context": "目标是收紧口径，不规定 SQL 写法或处理顺序。",
        "tool_evidence": _format_evidence(lines, evidence_style),
        "evidence_by_field": _build_evidence_by_field(lines, evidence_style, expect_unresolved),
        "gold_values": values,
        "expect_unresolved": sorted(expect_unresolved),
        "expect_not_applicable": sorted(expect_not_applicable),
    }


def _base_values(
    entity_key: str, role: str, time_field: str, fact_source: str, dimension_source: str
) -> dict[str, dict[str, Any]]:
    return {
        "feature_key": {
            "components": [{"field_ref": entity_key, "semantic_role": role, "null_semantics": "reject_record"}],
            "uniqueness_scope": "within_statistic_period",
        },
        "statistic_period": {
            "time_field": time_field,
            "grain": "day",
            "timezone": "Asia/Shanghai",
            "calendar": "gregorian",
            "week_start": None,
            "custom_definition": None,
        },
        "fact_filters": {
            "predicates": [
                {
                    "source_ref": fact_source,
                    "field_ref": "record_status",
                    "operator": "in",
                    "operand_type": "string",
                    "operands": ["valid", "settled"],
                    "null_semantics": "exclude_unknown",
                    "filter_scope": "source_level",
                    "branch_ref": None,
                }
            ]
        },
        "fact_derived_fields": {
            "derived_fields": [
                {
                    "source_ref": fact_source,
                    "target_field": "amount_yuan",
                    "input_fields": ["amount_cent"],
                    "expression": "amount_cent / 100",
                    "output_type": "decimal(18,2)",
                    "unit": "CNY_yuan",
                    "rounding": None,
                    "null_semantics": "propagate_null",
                }
            ]
        },
        "fact_deduplication": {
            "record_identity": {"fields": ["business_id", "line_id"], "null_equality": "reject_record"},
            "record_selection": {
                "criteria": [
                    {
                        "field_ref": "updated_at",
                        "choose": "greatest",
                        "value_type": "datetime",
                        "null_rank": "lowest",
                    },
                    {
                        "field_ref": "ingestion_id",
                        "choose": "greatest",
                        "value_type": "number",
                        "null_rank": "lowest",
                    },
                ],
                "if_all_criteria_equal": "conflict",
            },
        },
        "dimension_filters": {
            "scopes": [
                {
                    "source_ref": dimension_source,
                    "predicates": [
                        {
                            "field_ref": "is_active",
                            "operator": "eq",
                            "operand_type": "boolean",
                            "operands": ["true"],
                            "null_semantics": "exclude_unknown",
                        }
                    ],
                }
            ]
        },
        "dimension_derived_fields": {
            "scopes": [
                {
                    "source_ref": dimension_source,
                    "derived_fields": [
                        {
                            "target_field": "name_norm",
                            "input_fields": ["name"],
                            "expression": "lower(trim(name))",
                            "output_type": "string",
                            "unit": None,
                            "rounding": None,
                            "null_semantics": "propagate_null",
                        }
                    ],
                }
            ]
        },
        "dimension_deduplication": {
            "scopes": [
                {
                    "source_ref": dimension_source,
                    "record_identity": {"fields": [entity_key], "null_equality": "reject_record"},
                    "record_selection": {
                        "criteria": [
                            {
                                "field_ref": "version_no",
                                "choose": "greatest",
                                "value_type": "number",
                                "null_rank": "lowest",
                            }
                        ],
                        "if_all_criteria_equal": "conflict",
                    },
                }
            ]
        },
        "dimension_relations": {
            "relations": [
                {
                    "fact_source_ref": fact_source,
                    "dimension_source_ref": dimension_source,
                    "join_type": "left",
                    "conditions": [
                        {
                            "fact_field": entity_key,
                            "operator": "eq",
                            "dimension_field": entity_key,
                            "null_matches": False,
                        }
                    ],
                    "cardinality": "many_to_one",
                    "unmatched_fact": "retain_with_nulls",
                    "temporal_validity": None,
                }
            ]
        },
        "time_window": {
            "time_field": time_field,
            "anchor": {"field": None, "value": None},
            "range": {
                "type": "calendar_periods",
                "direction": "past",
                "number": 7,
                "unit": "day",
                "include_anchor_period": False,
            },
            "boundary": {"start_inclusive": True, "end_inclusive": False},
            "null_time_semantics": "exclude_record",
        },
        "final_sequence_partitioning": {
            "partition_fields": [entity_key],
            "null_equality": "reject_record",
        },
        "final_sequence_ordering": {
            "criteria": [
                {
                    "field_ref": time_field,
                    "direction": "ascending",
                    "value_type": "datetime",
                    "null_rank": "reject_record",
                },
                {
                    "field_ref": "event_id",
                    "direction": "ascending",
                    "value_type": "string",
                    "null_rank": "highest",
                },
            ],
            "tie_behavior": "records_are_equivalent",
        },
        "final_sequence_deduplication": {
            "identity_ref": "feature_key",
            "custom_identity_fields": [],
            "on_duplicate": "choose_one",
            "record_selection": {
                "criteria": [
                    {
                        "field_ref": "score",
                        "choose": "greatest",
                        "value_type": "number",
                        "null_rank": "lowest",
                    }
                ],
                "if_all_criteria_equal": "conflict",
            },
        },
    }


def _base_evidence_lines(
    entity_key: str, role: str, time_field: str, fact_source: str, dimension_source: str
) -> list[str]:
    return [
        f"最终特征主键是 {entity_key}，语义角色是{role}；空值记录拒绝；在每个统计周期内唯一。",
        f"输出按 {time_field} 的自然日分桶；时区 Asia/Shanghai；gregorian 日历。",
        f"事实源 {fact_source} 只取 record_status in ('valid','settled')；record_status 为空时排除。",
        f"{fact_source}.amount_cent / 100 得到 amount_yuan，输出 decimal(18,2)，单位 CNY_yuan，"
        "不舍入，输入为空则输出为空。",
        "事实记录以 business_id + line_id 判定相同，任一为空拒绝；先取 updated_at 最大"
        "（datetime，空值最低），再取 ingestion_id 最大（number，空值最低），仍相同则冲突。",
        f"维度源 {dimension_source} 只取 is_active = true，空值排除。",
        f"{dimension_source}.name 经 lower(trim(name)) 得到 name_norm，输出 string，空值保持空，无单位和舍入。",
        f"{dimension_source} 以 {entity_key} 判定同一实体，空值拒绝；按 version_no 最大选择"
        "（number，空值最低），仍相同则冲突。",
        f"{fact_source} left join {dimension_source} on {fact_source}.{entity_key} = "
        f"{dimension_source}.{entity_key}；null 不匹配；many_to_one；未匹配事实保留且维度列为空；"
        "不是时间有效关联。",
        f"窗口用 {time_field} 判定，过去 7 个完整自然日，不含今天，左闭右开，空时间排除。",
        f"每个 {entity_key} 形成一个逻辑序列；分区字段为空时拒绝记录。",
        f"序列先按 {time_field} 升序（datetime，空值拒绝），再按 event_id 升序"
        "（string，空值最高）；仍相同视为等价记录。",
        "最终序列按 feature_key 判重；重复时取 score 最大（number，空值最低），仍相同则冲突。",
        f"注意：{fact_source}.row_id 是源表物理键，不是最终特征主键，也不是逻辑序列分区键。",
    ]


def _apply_incomplete_variant(
    index: int,
    values: dict[str, dict[str, Any]],
    lines: list[str],
    expect_unresolved: set[str],
) -> None:
    variant = (index - 75) % 5
    if variant == 0:
        time_window = values.get("time_window", {})
        time_field = time_window.get("time_field")
        lines[9] = f"窗口只确认使用 {time_field}，取过去 7 天；自然周期或连续时长、边界和空值行为未确认。"
        time_window["anchor"] = {"field": None, "value": None}
        time_window["range"] = {
            "type": None,
            "direction": "past",
            "number": 7,
            "unit": "day",
            "include_anchor_period": None,
        }
        time_window["boundary"] = {"start_inclusive": None, "end_inclusive": None}
        time_window["null_time_semantics"] = None
        expect_unresolved.add("time_window")
    elif variant == 1:
        lines[4] = "事实记录以 business_id + line_id 判定相同，任一为空拒绝；同组记录的选择规则未确认。"
        dedup = values.get("fact_deduplication", {})
        dedup["record_selection"] = {"criteria": [], "if_all_criteria_equal": None}
        expect_unresolved.add("fact_deduplication")
    elif variant == 2:
        relation = values.get("dimension_relations", {}).get("relations", [])[0]
        condition = relation.get("conditions", [])[0]
        lines[8] = (
            f"{relation.get('fact_source_ref')} 关联 {relation.get('dimension_source_ref')} on "
            f"{relation.get('fact_source_ref')}.{condition.get('fact_field')} = "
            f"{relation.get('dimension_source_ref')}.{condition.get('dimension_field')}；"
            "关联类型、null 匹配、基数和未匹配事实行为未确认；不是时间有效关联。"
        )
        relation["join_type"] = None
        relation.get("conditions", [])[0]["null_matches"] = None
        relation["cardinality"] = None
        relation["unmatched_fact"] = None
        expect_unresolved.add("dimension_relations")
    elif variant == 3:
        lines[1] = lines[1].split("；")[0] + "；时区和日历未确认。"
        period = values.get("statistic_period", {})
        period["timezone"] = None
        period["calendar"] = None
        expect_unresolved.add("statistic_period")
    else:
        ordering = values.get("final_sequence_ordering", {})
        time_field = ordering.get("criteria", [])[0].get("field_ref")
        lines[11] = f"序列使用 {time_field} 和 event_id 排序，但方向、类型、空值位置和平局行为未确认。"
        ordering["criteria"] = [
            {
                "field_ref": ordering.get("criteria", [])[0].get("field_ref"),
                "direction": None,
                "value_type": None,
                "null_rank": None,
            },
            {"field_ref": "event_id", "direction": None, "value_type": None, "null_rank": None},
        ]
        ordering["tie_behavior"] = None
        expect_unresolved.add("final_sequence_ordering")


def _apply_optional_variant(
    index: int,
    values: dict[str, dict[str, Any]],
    lines: list[str],
    expect_not_applicable: set[str],
) -> None:
    variant = (index - 60) % 4
    if variant in {0, 3}:
        lines[2] = "[evidence_status: explicit_none] 工具明确确认事实表不需要额外过滤。"
        values["fact_filters"] = {"predicates": []}
        expect_not_applicable.add("fact_filters")
    if variant in {1, 3}:
        lines[3] = "[evidence_status: explicit_none] 工具明确确认事实字段不需要转换。"
        values["fact_derived_fields"] = {"derived_fields": []}
        expect_not_applicable.add("fact_derived_fields")
    if variant in {2, 3}:
        lines[4] = "[evidence_status: explicit_none] 工具明确确认事实记录不需要去重。"
        values["fact_deduplication"] = {
            "record_identity": {"fields": [], "null_equality": None},
            "record_selection": {"criteria": [], "if_all_criteria_equal": None},
        }
        expect_not_applicable.add("fact_deduplication")


def _apply_conflict_variant(
    index: int,
    values: dict[str, dict[str, Any]],
    lines: list[str],
    expect_unresolved: set[str],
) -> None:
    variant = (index - 90) % 5
    if variant == 0:
        lines.append("工具 A 说明时间窗口包含今天；工具 B 说明同一窗口不包含今天；两者没有优先级。")
        values.get("time_window", {}).get("range", {})["include_anchor_period"] = None
        expect_unresolved.add("time_window")
    elif variant == 1:
        lines.append("工具 A 给统计周期时区 Asia/Shanghai；工具 B 给统计周期时区 UTC；两者没有优先级。")
        values.get("statistic_period", {})["timezone"] = None
        expect_unresolved.add("statistic_period")
    elif variant == 2:
        lines.append("工具 A 要求 left join；工具 B 要求 inner join；两者没有优先级。")
        values.get("dimension_relations", {}).get("relations", [])[0]["join_type"] = None
        expect_unresolved.add("dimension_relations")
    elif variant == 3:
        lines.append("工具 A 要求最终重复记录 choose_one；工具 B 要求 retain_all；两者没有优先级。")
        final_dedup = values.get("final_sequence_deduplication", {})
        final_dedup["on_duplicate"] = None
        final_dedup["record_selection"] = {"criteria": [], "if_all_criteria_equal": None}
        expect_unresolved.add("final_sequence_deduplication")
    else:
        original_key = values.get("feature_key", {}).get("components", [])[0].get("field_ref")
        lines[0] = "特征主键的唯一范围已独立确认为 within_statistic_period；具体主键字段见后续冲突证据。"
        lines.append(f"工具 A 说最终特征主键是 {original_key}；工具 B 说最终特征主键是 business_id；无优先级。")
        values.get("feature_key", {})["components"] = []
        values.get("feature_key", {})["uniqueness_scope"] = "within_statistic_period"
        expect_unresolved.add("feature_key")


def _format_evidence(lines: list[str], style: int) -> str:
    copied = [line for line in deepcopy(lines) if line]
    if not copied:
        return ""
    if style == 0:
        return "\n".join(f"- {line}" for line in copied)
    if style == 1:
        return "\n".join(f"证据 {number + 1}：{line}" for number, line in enumerate(copied))
    if style == 2:
        return "工具召回结果如下。" + " ".join(copied)
    return "\n".join(f"item_{number + 1}: {line}" for number, line in enumerate(copied))


def _build_evidence_by_field(lines: list[str], style: int, expect_unresolved: set[str]) -> dict[str, str]:
    routed = {}
    conflict_lines = lines[14:] if len(lines) > 14 else []
    for field_id, indexes in FIELD_EVIDENCE_LINE_INDEXES.items():
        selected = [lines[index] for index in indexes if index < len(lines) and lines[index]]
        if field_id in expect_unresolved:
            selected.extend(conflict_lines)
        routed[field_id] = _format_evidence(selected, style)
    return routed
