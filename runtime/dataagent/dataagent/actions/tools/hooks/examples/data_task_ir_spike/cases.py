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
"""Deterministic 100-case benchmark generator for the DataTaskIR spike (v1)."""

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

# Evidence line indexes for the 6 v1 field units.
FIELD_EVIDENCE_LINE_INDEXES = {
    "identity": [0, 6],
    "sources": [2, 3, 4, 5],
    "time_context": [1, 7],
    "output_features": [8],
    "pipeline": [9],
    "sequence": [10, 11, 12],
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
        "identity": {
            "components": [
                {"field_ref": entity_key, "semantic_role": role, "null_semantics": "reject_record"}
            ],
            "uniqueness_scope": "within_statistic_period",
        },
        "sources": {
            "facts": [
                {
                    "source_ref": fact_source,
                    "filters": [
                        {
                            "field_ref": "record_status",
                            "operator": "in",
                            "operand_type": "string",
                            "operands": ["valid", "settled"],
                            "null_semantics": "exclude_unknown",
                            "filter_scope": "source_level",
                            "branch_ref": None,
                        }
                    ],
                    "derived": [
                        {
                            "target_field": "amount_yuan",
                            "input_fields": ["amount_cent"],
                            "expression": "amount_cent / 100",
                            "output_type": "decimal(18,2)",
                            "unit": "CNY_yuan",
                            "rounding": None,
                            "null_semantics": "propagate_null",
                        }
                    ],
                    "dedup": {
                        "record_identity": {
                            "fields": ["business_id", "line_id"],
                            "null_equality": "reject_record",
                        },
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
                }
            ],
            "dimensions": [
                {
                    "source_ref": dimension_source,
                    "filters": [
                        {
                            "field_ref": "is_active",
                            "operator": "eq",
                            "operand_type": "boolean",
                            "operands": ["true"],
                            "null_semantics": "exclude_unknown",
                        }
                    ],
                    "derived": [
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
                    "dedup": {
                        "record_identity": {
                            "fields": [entity_key],
                            "null_equality": "reject_record",
                        },
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
                    },
                    "join": {
                        "fact_source_ref": fact_source,
                        "join_type": "left",
                        "conditions": [
                            {
                                "fact_field": entity_key,
                                "operator": "eq",
                                "dim_field": entity_key,
                                "null_matches": False,
                            }
                        ],
                        "cardinality": "many_to_one",
                        "unmatched_fact": "keep",
                        "temporal_validity": None,
                    },
                }
            ],
        },
        "time_context": {
            "output_bucket": {
                "time_field": time_field,
                "grain": "day",
                "timezone": "Asia/Shanghai",
                "calendar": "gregorian",
                "week_start": None,
                "custom_definition": None,
            },
            "input_windows": [
                {
                    "window_id": "w7d",
                    "time_field": time_field,
                    "anchor": "current_partition",
                    "range": {
                        "type": "calendar_periods",
                        "direction": "past",
                        "number": 7,
                        "unit": "day",
                        "include_anchor_period": False,
                    },
                    "boundary": "half_open_right",
                }
            ],
        },
        "output_features": {
            "layout": "flat",
            "axes": [],
            "measures": [],
            "overrides": [],
            "extras": [],
            "features": [
                {
                    "field_name": "amount_yuan_sum",
                    "role": "metric",
                    "output_type": "decimal(18,2)",
                    "description": "金额合计",
                    "source": f"{fact_source}.amount_yuan",
                    "calc": {"intent": "金额求和", "on": "amount_yuan"},
                    "aggregation": {
                        "grain": [entity_key],
                        "count_type": None,
                        "count_entity": None,
                        "distinct_field": None,
                        "expression_template": None,
                    },
                    "filters": [],
                    "window_ref": "w7d",
                    "ratio": None,
                    "rank": None,
                    "depends_on": [],
                }
            ],
        },
        "pipeline": {
            "deduplication_before_join": True,
            "deduplication_before_aggregation": True,
            "operation_order": ["fact_filter", "fact_dedup", "join", "aggregate", "sequence"],
        },
        "sequence": {
            "partition_fields": [entity_key],
            "null_equality": "reject_record",
            "ordering": [
                {
                    "field_ref": time_field,
                    "direction": "asc",
                    "value_type": "datetime",
                    "null_rank": "reject_record",
                },
                {
                    "field_ref": "event_id",
                    "direction": "asc",
                    "value_type": "string",
                    "null_rank": "highest",
                },
            ],
            "dedup": {
                "identity_ref": "identity",
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
            "truncation": None,
            "window_partition": None,
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
        f"维度源 {dimension_source} 只取 is_active = true，空值排除；"
        f"{dimension_source}.name 经 lower(trim(name)) 得到 name_norm；"
        f"以 {entity_key} 判定同一实体，按 version_no 最大选择；"
        f"{fact_source} left join {dimension_source} on {entity_key}；many_to_one；未匹配事实保留。",
        f"注意：{fact_source}.row_id 是源表物理键，不是最终特征主键，也不是逻辑序列分区键。",
        f"输入回看窗口 w7d 用 {time_field} 判定，过去 7 个完整自然日，不含今天，左闭右开。",
        f"输出特征 amount_yuan_sum：近7天金额求和，按 {entity_key} 聚合，窗口引用 w7d。",
        "执行顺序：先事实过滤与去重，再 JOIN，再聚合，最后序列后处理；事实去重在 JOIN 前。",
        f"每个 {entity_key} 形成一个逻辑序列；分区字段为空时拒绝记录。",
        f"序列先按 {time_field} 升序（datetime，空值拒绝），再按 event_id 升序"
        "（string，空值最高）；仍相同视为等价记录。",
        "最终序列按 identity 判重；重复时取 score 最大（number，空值最低），仍相同则冲突。",
    ]


def _apply_incomplete_variant(
    index: int,
    values: dict[str, dict[str, Any]],
    lines: list[str],
    expect_unresolved: set[str],
) -> None:
    variant = (index - 75) % 5
    if variant == 0:
        window = values["time_context"]["input_windows"][0]
        time_field = window.get("time_field")
        lines[7] = f"窗口 w7d 只确认使用 {time_field}，取过去 7 天；自然周期或连续时长、边界未确认。"
        window["range"] = {
            "type": None,
            "direction": "past",
            "number": 7,
            "unit": "day",
            "include_anchor_period": None,
        }
        window["boundary"] = None
        expect_unresolved.add("time_context")
    elif variant == 1:
        lines[4] = "事实记录以 business_id + line_id 判定相同，任一为空拒绝；同组记录的选择规则未确认。"
        values["sources"]["facts"][0]["dedup"]["record_selection"] = {
            "criteria": [],
            "if_all_criteria_equal": None,
        }
        expect_unresolved.add("sources")
    elif variant == 2:
        join = values["sources"]["dimensions"][0]["join"]
        lines[5] = (
            f"{join.get('fact_source_ref')} 关联 {values['sources']['dimensions'][0].get('source_ref')}；"
            "关联类型、基数和未匹配事实行为未确认。"
        )
        join["join_type"] = None
        join["cardinality"] = None
        join["unmatched_fact"] = None
        expect_unresolved.add("sources")
    elif variant == 3:
        lines[1] = lines[1].split("；")[0] + "；时区和日历未确认。"
        values["time_context"]["output_bucket"]["timezone"] = None
        values["time_context"]["output_bucket"]["calendar"] = None
        expect_unresolved.add("time_context")
    else:
        ordering = values["sequence"]["ordering"]
        time_field = ordering[0].get("field_ref")
        lines[11] = f"序列使用 {time_field} 和 event_id 排序，但方向、类型、空值位置未确认。"
        values["sequence"]["ordering"] = [
            {"field_ref": time_field, "direction": None, "value_type": None, "null_rank": None},
            {"field_ref": "event_id", "direction": None, "value_type": None, "null_rank": None},
        ]
        expect_unresolved.add("sequence")


def _apply_optional_variant(
    index: int,
    values: dict[str, dict[str, Any]],
    lines: list[str],
    expect_not_applicable: set[str],
) -> None:
    variant = (index - 60) % 4
    if variant in {0, 3}:
        lines[2] = "[evidence_status: explicit_none] 工具明确确认事实表不需要额外过滤。"
        values["sources"]["facts"][0]["filters"] = []
    if variant in {1, 3}:
        lines[3] = "[evidence_status: explicit_none] 工具明确确认事实字段不需要转换。"
        values["sources"]["facts"][0]["derived"] = []
    if variant in {2, 3}:
        lines[4] = "[evidence_status: explicit_none] 工具明确确认事实记录不需要去重。"
        values["sources"]["facts"][0]["dedup"] = None
    if variant == 3:
        expect_not_applicable.add("sources")


def _apply_conflict_variant(
    index: int,
    values: dict[str, dict[str, Any]],
    lines: list[str],
    expect_unresolved: set[str],
) -> None:
    variant = (index - 90) % 5
    if variant == 0:
        lines.append("工具 A 说明时间窗口包含今天；工具 B 说明同一窗口不包含今天；两者没有优先级。")
        values["time_context"]["input_windows"][0]["range"]["include_anchor_period"] = None
        expect_unresolved.add("time_context")
    elif variant == 1:
        lines.append("工具 A 给统计周期时区 Asia/Shanghai；工具 B 给统计周期时区 UTC；两者没有优先级。")
        values["time_context"]["output_bucket"]["timezone"] = None
        expect_unresolved.add("time_context")
    elif variant == 2:
        lines.append("工具 A 要求 left join；工具 B 要求 inner join；两者没有优先级。")
        values["sources"]["dimensions"][0]["join"]["join_type"] = None
        expect_unresolved.add("sources")
    elif variant == 3:
        lines.append("工具 A 要求最终重复记录 choose_one；工具 B 要求 retain_all；两者没有优先级。")
        values["sequence"]["dedup"]["on_duplicate"] = None
        values["sequence"]["dedup"]["record_selection"] = {
            "criteria": [],
            "if_all_criteria_equal": None,
        }
        expect_unresolved.add("sequence")
    else:
        original_key = values["identity"]["components"][0]["field_ref"]
        lines[0] = "特征主键的唯一范围已独立确认为 within_statistic_period；具体主键字段见后续冲突证据。"
        lines.append(
            f"工具 A 说最终特征主键是 {original_key}；工具 B 说最终特征主键是 business_id；无优先级。"
        )
        values["identity"]["components"] = []
        values["identity"]["uniqueness_scope"] = "within_statistic_period"
        expect_unresolved.add("identity")


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
    conflict_lines = lines[13:] if len(lines) > 13 else []
    for field_id, indexes in FIELD_EVIDENCE_LINE_INDEXES.items():
        selected = [lines[index] for index in indexes if index < len(lines) and lines[index]]
        if field_id in expect_unresolved:
            selected.extend(conflict_lines)
        routed[field_id] = _format_evidence(selected, style)
    return routed
