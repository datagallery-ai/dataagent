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

import itertools
import json
import logging
import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)
_OMITTED = object()

FIELD_TITLES = {
    "identity": "特征主键",
    "sources": "数据源与关联预处理",
    "time_context": "时间上下文",
    "output_features": "输出特征目录",
    "pipeline": "执行管道",
    "sequence": "最终序列后处理",
}
# User-facing HITL titles: business language, not IR field_id / schema jargon.
# Keep in sync with hitl_resolver_assets._IR_HITL_*_TITLES for auto-resolver routing.
IR_HITL_USER_TITLES = {
    "identity": "统计主键",
    "sources": "数据来源与关联",
    "time_context": "时间范围",
    "output_features": "输出指标",
    "pipeline": "计算步骤",
    "sequence": "排序与截断",
}

# Only these fields may block IR consumers via HITL; other unresolved stay warn-only.
IR_HITL_BLOCKING_FIELDS = frozenset(
    {
        "identity",
        "sources",
        "time_context",
        "output_features",
    }
)
# Ask order when multiple critical fields are unresolved (one HITL turn at a time).
IR_HITL_FIELD_PRIORITY = (
    "identity",
    "sources",
    "time_context",
    "output_features",
)
# Routing marker for auto-resolver (kept in tool reason); user-facing pending uses
# IR_HITL_USER_PENDING_MARKER instead so interactive users do not see "DataTaskIR".
IR_HITL_PENDING_MARKER = "DataTaskIR 未确认口径"
IR_HITL_USER_PENDING_MARKER = "待确认业务口径"
# Pre-SQL final review (distinct from unresolved追问); keep in sync with hitl_resolver_assets.
IR_CALIBER_REVIEW_MARKER = "DataTaskIR 口径最终检视"
IR_CALIBER_REVIEW_USER_MARKER = "口径最终检视"

# Replace IR schema paths / keys in user-visible HITL text (order: longer paths first).
# Use ASCII/identifier lookarounds instead of \b: Python \b treats CJK as \w, so
# "identity.components缺失" would not match with word boundaries.
_IR_TOKEN_EDGE = r"A-Za-z0-9_."
_IR_HITL_JARGON_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"(?<![{_IR_TOKEN_EDGE}])(?:{pattern})(?![{_IR_TOKEN_EDGE}])"), replacement)
    for pattern, replacement in (
        (r"identity\.components(?:\[\])?(?:\.field_ref)?", "统计主键字段"),
        (r"components(?:\[\])?\.field_ref", "主键字段"),
        (r"components(?:\[\])?", "主键字段"),
        (r"output_features(?:\.features)?(?:\[\])?", "输出指标"),
        (r"time_context(?:\.input_windows)?(?:\[\])?", "时间范围"),
        (r"sources\.facts(?:\[\])?", "事实表"),
        (r"sources\.dimensions(?:\[\])?", "维表"),
        (r"aggregation\.grain", "聚合粒度"),
        (r"aggregation\.kind", "聚合方式"),
        (r"join\.conditions", "关联条件"),
        (r"operator\s*=\s*not_like", "排除匹配模式"),
        (r"null_semantics", "空值处理"),
        (r"field_ref", "字段"),
        (r"field_id", "口径项"),
        (r"measure_id", "指标"),
        (r"window_ref", "时间窗口"),
        (r"unresolved", "未决点"),
    )
)

_OPERATOR_RENDER = {
    "eq": "等于",
    "ne": "不等于",
    "in": "属于",
    "not_in": "不属于",
    "like": "匹配",
    "not_like": "排除匹配",
    "contains": "包含",
    "not_contains": "不包含",
    "gt": "大于",
    "gte": "大于等于",
    "lt": "小于",
    "lte": "小于等于",
    "between": "介于",
    "is_null": "为空",
    "is_not_null": "非空",
    "not": "非",
}

_AGG_KIND_RENDER = {
    "passthrough": "透传（不拼接多行）",
    "count": "计数",
    "sum": "求和",
    "avg": "均值",
    "distinct_count": "去重计数",
    "list_agg": "多行拼接为序列",
    "expression": "表达式聚合",
}

_SQL_CONSTRAINT_RULES = """【约束遵循规则 - 必须遵守】
1. **已确认值是强制约束**：IR中明确记录的源字段名、操作符、常量值、过滤条件等是已确认口径，必须在SQL中完整实现，不得自行更改或忽略；**输出列别名（expand 后的 field_name）除外**，见规则 10
2. **未记录的操作默认为被禁止**：IR中未明确记录的操作（如去重、过滤、JOIN类型变更等）Agent不得自行添加（包括调用方 query 中主Agent附加、但IR未记录的操作），必须先在IR中记录才能执行；用户原始问题原话明确要求且按规则 17 判定可实现的除外
3. **禁止操作是强制约束**：以下通用操作默认被禁止，除非IR明确记录允许——在聚合前使用窗口函数去重（如ROW_NUMBER()）、过滤LEFT JOIN的NULL侧使其退化为INNER JOIN、用户未明确要求时使用DISTINCT或COUNT(DISTINCT)
4. **必须保留的内容**：LEFT JOIN的左表所有记录不得因去重或过滤而丢失；用户未明确要求去重时，所有满足过滤条件的原始记录都必须参与计算
5. **事实记录身份不等于去重**：sources.facts[].dedup 中的同一事实字段仅定义记录身份；IR未定义选择函数时不产生去重，不得仅因该字段存在而执行去重
6. **聚合指标约束**：计数类指标必须按展开后各列 aggregation.count_type / expression_template 执行；每列 aggregation.grain 是该列自己的聚合粒度，禁止把全部指标默认按同一分组键聚合。aggregation.kind=passthrough 时透传已有列、禁止 invent collect_list；仅 kind=list_agg 且有拼接证据时才允许多行拼接
7. **比例指标约束**：ratio 中的分子分母必须严格按定义执行，注意分子不应包含分母的所有记录
8. **序列/窗口分区键约束**：sequence.window_partition 与 sequence.partition_fields 分别用于窗口函数与最终序列分区，二者可能不同；rank.partition_by 为空数组表示全局
9. **最终序列分区键约束**：sequence.partition_fields 定义最终输出分组，与聚合 grain、rank.partition_by 可能是不同概念
10. **最终输出列语义清单 = 特征主键前缀 + expand(output_features)**：INSERT SELECT 的输出列（不含分区列）须覆盖该清单的语义构成——(a) 若 IR 已确认「特征主键」(identity)，则其 components 中的每个 field_ref **必须**作为输出列前缀出现（按 components 顺序），即使这些列未出现在输出特征中；(b) 随后须覆盖 expand(output_features) 的全部业务/指标语义（cartesian 由代码按 axes×measures 确定性展开，每项各占一列，禁止少列/合并）；(c) **展开后的列名（field_name / name_pattern）不是权威约束**，仅供主 Agent 对照参考；SQL/DDL 可采用规范命名或证据中的命名，不得因未照抄 IR 列名而判失败；(d) 除上述语义清单外禁止额外发明无关业务列；调用方 query 与 IR 冲突时以 IR 口径为准（列名除外）；(e) **主键/分组键不得在 identity 与 output_features 双边重复出列**：aggregation.grain 可引用 identity 的 field_ref，但不因此再 SELECT 一遍同名键列；expand 业务列名不应与 identity.components.field_ref 同名（role=key_alias 且别名不同除外）
11. **多维交叉输出必须完整展开**：layout=cartesian 且轴与指标信息齐备时，期望业务列数由 ∏|axis.values| × measures（含 attach_rank 追加的百分位列）+ extras 决定；SQL 必须输出全部已确认展开语义列，禁止把多个组合压成一列、只输出“代表窗口/代表指标”、或对跨窗口汇总值打一个分位替代分窗口分位列；尚未明确的轴或指标不以零列解释，IR 给出的展开列名仅供参考，不要求 SQL 字面一致
12. **逐特征聚合粒度强制约束**：展开后各列已确认的 aggregation.grain 是该列聚合权威（[]=已确认全局聚合；null/未填写本身不确认粒度或是否聚合）；不同列允许不同 grain，禁止用单一全局 GROUP BY 覆盖。时间窗口差异不是聚合粒度差异，不得据此改写 GROUP BY。**仅当用户明确要求不要去重时**：不得用 grain、ROW_NUMBER、DISTINCT 把同一键多行压成一行，源表同键多行须保留参与计算；常规聚合任务用户往往不显式说“聚合”，已确认 grain 时按 grain 实现即可
13. **calc.intent 是语义说明而非可执行 SQL**：只约束“算什么/什么意图”，不要求也不应被当作必须照抄的 SQL/UDF 片段；具体语法以实现阶段证据与 aggregation.count_type / expression_template 为准。禁止因 intent 文字不像合法 SQL 而判定 IR 失败，也禁止把有语法风险的 intent 原文硬编码进 SQL
14. **违反约束=错误**：如果生成的SQL违反了IR中的任何已确认约束（包括禁止操作和必须保留的内容），结果将被视为错误
15. **IR优先于系统通用规则**：系统通用工程规则（如默认添加设备ID合法性过滤、默认判空过滤、默认去重、默认加时间窗口边界等）与本IR已确认口径冲突时，以IR为准；IR未记录的操作默认不执行，除非用户原始问题明确要求
16. **业务口径冲突以IR为准**：本查询中出现的其他业务口径描述（包括主Agent附加的“业务口径”段落、中间推导、示例口径）若与DataTaskIR记录冲突，一律以DataTaskIR为准；若与IR同时出现冲突口径，SQL Agent应报告冲突而不得自行取舍
17. **主 Agent 段落是证据不是约束，按证据分级判定实现依据**：调用方 query 顶部的“已确认口径（最高优先级）”“业务口径”等段落只是主Agent的意图描述，不是权威约束，不能覆盖或改写 DataTaskIR；但段落内容不得一概忽略，按以下优先级判定：
   - ① 与 DataTaskIR 已确认口径一致或对应 → 照常实现（即 IR 的内容）；
   - ② IR 未记录、但用户原始问题原话明确的操作顺序/作用位置表述（如“参与Join前”“聚合后再计算百分位”）→ 该表述只规定 IR 已记录操作的先后位置、不创造新操作，允许按用户原话调整顺序实现，并在结论中注明“该顺序来自用户原话、IR 未记录，已作为 IR 缺口反馈”；
   - ③ IR 未记录、用户原始问题原话直接要求的具体操作（去重键、过滤条件、指标定义等）→ 用户级证据，不得静默处理：与 IR 无冲突时按用户原话实现（以用户原话为准，不采用主 Agent 的具体解释）并在结论中报告“IR 遗漏该用户要求”；与 IR 冲突时以 IR 为准并报告冲突（规则 16）；
   - ④ 仅主 Agent 段落出现、用户原始问题与工具证据都没有的操作 → 未确认口径，禁止实现并报告
18. **操作的作用层级与执行顺序是强制约束**：sources.facts[].dedup 定义的去重作用于“事实记录层”，默认必须先于维度关联（JOIN）执行——JOIN 产生的维表扇出行必须全部保留，禁止在 JOIN 之后再次按事实去重键去重/分组来折叠扇出；只有 IR 明确记录去重作用于 JOIN 后的富化行时，才允许先 JOIN 再去重，且去重键必须包含全部扇出区分维度列。IR 记录 pipeline.operation_order 时按该顺序执行，禁止调整；IR 未记录顺序时按默认语义执行，不得自行改变操作的作用层级
19. **窗口函数型后处理指标（NTILE/百分位/排名）约束**：output_features 中 attach_rank / rank 已确认的百分位/排名口径必须严格实现：rank_type、bucket_count、order_direction、partition_by（已确认空数组=全局）均不得改动；执行位置以 pipeline.operation_order 为准，未记录时只依据已确认指标依赖确定，无法确定时核对，不默认添加 aggregate。cartesian 下 attach_rank 随轴展开，每个轴组合各产生独立百分位列——禁止对跨窗口汇总值打百分位来替代分窗口百分位列，禁止少列或合并
20. **允许信息不完备**：整个字段或子字段未确认时允许留空，不得为了填满 IR 猜测业务值、添加操作或要求用户逐项补齐模板。未知值不等于已确认不执行；空数组按具体字段协议解释，不得据此否定用户原话已有的要求。生成与校验只强制遵循已确认部分；影响当前任务正确实现的真实缺失须检索或澄清，不能静默猜测。
"""


def render_constraint_context(
    field_outputs: list[dict[str, Any]],
    *,
    warnings: list[str] | None = None,
) -> str:
    """Render the complete consumer contract; keep policy out of SQL tool adapters."""
    if not field_outputs:
        return ""
    intro = (
        "【DataTaskIR 强制约束说明】\n"
        "以下DataTaskIR记录了此任务的**已确认口径约束**，你生成的SQL**必须严格遵循**：\n\n"
    )
    # Retain the warnings parameter for diagnostics compatibility; warnings stay
    # in logs/trace/snapshots and do not change the rendered constraint contract.
    return intro + render_context(field_outputs) + "\n\n" + _SQL_CONSTRAINT_RULES


def render_context(field_outputs: list[dict[str, Any]]) -> str:
    """Render only confirmed DataTaskIR leaves into stable NL2SQL query context."""
    by_id = {str(output.get("field_id", "")): output for output in field_outputs}
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
        _log_omissions(field_id, unresolved, question, omitted_paths)
        if unresolved:
            question_text = question if isinstance(question, str) and question.strip() else ""
            warning = f"{FIELD_TITLES.get(field_id)}：存在未确认口径"
            if question_text:
                warning += f"（待澄清问题：{question_text}）"
            warnings.append(warning)
        if not value:
            continue
        renderer = _RENDERERS.get(field_id, _render_json_value)
        value_text = renderer(value)
        if value_text:
            lines.append(f"- {FIELD_TITLES.get(field_id)}：{value_text}")
    output_contract = _render_final_output_column_contract(by_id)
    if output_contract:
        lines.append(f"- 最终输出列构成（语义权威；列名仅供参考）：{output_contract}")
    grain_contract = _render_per_field_aggregation_grain_contract(by_id)
    if grain_contract:
        lines.append(f"- 逐特征聚合粒度（权威）：{grain_contract}")
    if not _has_confirmed_dedup(by_id) and any(
        by_id.get(field_id) for field_id in ("sources", "sequence")
    ):
        lines.append(
            "- 去重约束：本任务未确认任何去重要求（sources 事实/维度 dedup、sequence.dedup 均无已确认内容）。"
            "用户未明确要求去重时，禁止对事实记录或最终输出执行去重；源表为天级增量表或存在多分区不作为去重依据。"
            "维表在 JOIN 前为保证关联键唯一所必需的去重按 JOIN 安全规则执行，不在此限。"
        )
    return "\n".join(lines)


def expand_output_features(value: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Deterministically expand output_features into concrete output columns.

    layout=flat → features[]; layout=cartesian → axes×measures (+attach_rank) + extras.
    """
    if not isinstance(value, dict):
        return []
    layout = value.get("layout")
    if layout == "flat":
        return [
            deepcopy(item)
            for item in (value.get("features") or [])
            if isinstance(item, dict) and item.get("field_name")
        ]
    if layout != "cartesian":
        return []

    axes = [axis for axis in (value.get("axes") or []) if isinstance(axis, dict)]
    measures = [m for m in (value.get("measures") or []) if isinstance(m, dict)]
    overrides = [o for o in (value.get("overrides") or []) if isinstance(o, dict)]
    extras = [
        deepcopy(item)
        for item in (value.get("extras") or [])
        if isinstance(item, dict) and item.get("field_name")
    ]

    axis_ids = [str(a.get("axis_id") or "") for a in axes]
    axis_values = [
        [str(v) for v in (a.get("values") or []) if v is not None and str(v).strip()] for a in axes
    ]
    if not measures:
        return extras
    if axes and any(not vals for vals in axis_values):
        return extras

    products = list(itertools.product(*axis_values)) if axes else [()]
    columns: list[dict[str, Any]] = []
    for combo in products:
        binding = {axis_ids[i]: combo[i] for i in range(len(axis_ids))}
        for measure in measures:
            col = _apply_measure(measure, binding, overrides)
            if col.get("field_name"):
                columns.append(col)
            attach = measure.get("attach_rank")
            if isinstance(attach, dict) and attach:
                rank_col = _derive_rank_column(col, measure, attach, binding)
                if rank_col.get("field_name"):
                    columns.append(rank_col)
    columns.extend(extras)
    return columns


def _format_name_pattern(
    pattern: str | None, measure_id: str, binding: dict[str, str], default: str
) -> str:
    text = pattern if isinstance(pattern, str) and pattern.strip() else default
    mapping = {"measure_id": measure_id, **binding}

    # Support both {window} and {measure_id} style placeholders.
    def replacer(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(mapping.get(key, match.group(0)))

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", replacer, text)


def _apply_override(
    measure: dict[str, Any], binding: dict[str, str], overrides: list[dict[str, Any]]
) -> dict[str, Any]:
    result = deepcopy(measure)
    measure_id = str(measure.get("measure_id") or "")
    for override in overrides:
        if str(override.get("measure_id") or "") != measure_id:
            continue
        ov_binding = override.get("binding") or {}
        if not isinstance(ov_binding, dict):
            continue
        if any(str(ov_binding.get(k, "")) != str(v) for k, v in binding.items() if k in ov_binding):
            continue
        if any(str(binding.get(k, "")) != str(v) for k, v in ov_binding.items()):
            continue
        patch = override.get("patch")
        if isinstance(patch, dict):
            result = _deep_merge(result, patch)
    return result


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _apply_measure(
    measure: dict[str, Any],
    binding: dict[str, str],
    overrides: list[dict[str, Any]],
) -> dict[str, Any]:
    patched = _apply_override(measure, binding, overrides)
    measure_id = str(patched.get("measure_id") or "measure")
    field_name = _format_name_pattern(
        patched.get("name_pattern"),
        measure_id,
        binding,
        "{measure_id}_{window}",
    )
    col: dict[str, Any] = {
        "field_name": field_name,
        "role": patched.get("role"),
        "output_type": patched.get("output_type"),
        "description": patched.get("description"),
        "source": patched.get("source"),
        "calc": deepcopy(patched.get("calc") or {}),
        "aggregation": deepcopy(patched.get("aggregation")),
        "filters": deepcopy(patched.get("filters") or []),
        "window_ref": binding.get("window"),
        "ratio": deepcopy(patched.get("ratio")),
        "rank": None,
        "depends_on": deepcopy(patched.get("depends_on") or []),
        "measure_id": measure_id,
        "axis_binding": dict(binding),
    }
    return col


def _derive_rank_column(
    base_col: dict[str, Any],
    measure: dict[str, Any],
    attach: dict[str, Any],
    binding: dict[str, str],
) -> dict[str, Any]:
    measure_id = str(measure.get("measure_id") or "measure")
    field_name = _format_name_pattern(
        attach.get("name_pattern"),
        measure_id,
        binding,
        "{measure_id}_{window}_pct",
    )
    based_on = str(base_col.get("field_name") or "")
    return {
        "field_name": field_name,
        "role": "rank",
        "output_type": "int",
        "description": f"{based_on} 的百分位/排名",
        "source": based_on,
        "calc": {"intent": f"对 {based_on} 做窗口排名/分位", "on": based_on},
        "aggregation": None,
        "filters": [],
        "window_ref": binding.get("window"),
        "ratio": None,
        "rank": {
            "rank_type": attach.get("rank_type"),
            "bucket_count": attach.get("bucket_count"),
            "order_by": based_on,
            "order_direction": attach.get("order_direction"),
            "partition_by": deepcopy(attach.get("partition_by")),
            "based_on": based_on,
        },
        "depends_on": [based_on],
        "measure_id": measure_id,
        "axis_binding": dict(binding),
    }


def _has_confirmed_dedup(by_id: dict[str, dict[str, Any]]) -> bool:
    sources = by_id.get("sources")
    if sources:
        value = sources.get("value", {})
        if isinstance(value, dict):
            for fact in value.get("facts") or []:
                if isinstance(fact, dict) and _dedup_confirmed(fact.get("dedup")):
                    return True
            for dim in value.get("dimensions") or []:
                if isinstance(dim, dict) and _dedup_confirmed(dim.get("dedup")):
                    return True
    sequence = by_id.get("sequence")
    if sequence:
        value = sequence.get("value", {})
        if isinstance(value, dict):
            dedup = value.get("dedup")
            if isinstance(dedup, dict) and (
                dedup.get("on_duplicate") is not None or dedup.get("identity_ref") is not None
            ):
                return True
    return False


def _dedup_confirmed(dedup: Any) -> bool:
    if not isinstance(dedup, dict):
        return False
    identity = dedup.get("record_identity") or {}
    selection = dedup.get("record_selection") or {}
    if isinstance(identity, dict) and identity.get("fields"):
        return True
    if isinstance(selection, dict) and selection.get("criteria"):
        return True
    return False


def _normalize_unresolved(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def collect_ir_hitl_questions(
    field_results: list[dict] | None,
    *,
    blocking_fields: frozenset[str] | set[str] | list[str] | None = None,
    max_items: int = 1,
) -> list[dict[str, Any]]:
    """Collect blocking IR clarifications for HITL (critical unresolved + question).

    Non-whitelist fields may still carry unresolved/question for diagnostics, but they
    do not enter the HITL queue and must not block nl2sql consumers.

    By default only the highest-priority unresolved field is queued (max_items=1) so
    HITL / auto-resolver answers one口径 at a time instead of a packed multi-ask.
    """
    allowed = (
        IR_HITL_BLOCKING_FIELDS
        if blocking_fields is None
        else frozenset(str(item).strip() for item in blocking_fields if str(item).strip())
    )
    items: list[dict[str, Any]] = []
    for output in field_results or []:
        if not isinstance(output, dict):
            continue
        field_id = str(output.get("field_id") or "").strip()
        if field_id not in allowed:
            continue
        unresolved = _normalize_unresolved(output.get("unresolved"))
        question = output.get("question")
        if not unresolved:
            continue
        if not isinstance(question, str) or not question.strip():
            continue
        items.append(
            {
                "field_id": field_id,
                "title": IR_HITL_USER_TITLES.get(
                    field_id, FIELD_TITLES.get(field_id, field_id or "未命名字段")
                ),
                "question": question.strip(),
                "unresolved": unresolved,
            }
        )
    priority = {field_id: index for index, field_id in enumerate(IR_HITL_FIELD_PRIORITY)}
    items.sort(key=lambda item: priority.get(str(item.get("field_id") or ""), 99))
    if max_items is not None and max_items > 0:
        items = items[:max_items]
    return items


def humanize_ir_hitl_text(text: str) -> str:
    """Rewrite IR schema jargon in user-visible HITL copy (paths/keys → business phrasing)."""
    result = str(text or "")
    if not result:
        return ""
    for pattern, replacement in _IR_HITL_JARGON_REPLACEMENTS:
        result = pattern.sub(replacement, result)
    return result


def _extract_choice_hints(question: str) -> list[str]:
    """Best-effort parse of A/B choices from fill questions (还是/或/)."""
    text = (question or "").strip()
    if not text:
        return []
    for sep in (" 还是 ", "还是", " 或 ", "或者", " / ", "/"):
        if sep in text:
            parts = [part.strip(" ，,。.?？:：;；") for part in text.split(sep)]
            hints = [part for part in parts if 1 < len(part) <= 80]
            if len(hints) >= 2:
                return hints[:3]
    return []


def format_ir_hitl_pending_action(questions: list[dict[str, Any]], *, max_chars: int = 2000) -> str:
    """Build request_human_feedback pending_action text from IR clarification queue.

    User-facing copy is decoupled from IR schema: business titles, humanized
    questions, and no raw unresolved schema notes. Auto-resolver routing markers
    stay in the tool ``reason`` (see ir_question_guard), not in this body.
    """
    if not questions:
        return ""
    lines = [
        f"{IR_HITL_USER_PENDING_MARKER}：",
    ]
    for item in questions:
        field_id = str(item.get("field_id") or "").strip()
        title = (
            item.get("title")
            or IR_HITL_USER_TITLES.get(field_id)
            or FIELD_TITLES.get(field_id)
            or "未命名项"
        )
        question = humanize_ir_hitl_text(str(item.get("question") or "").strip())
        lines.append(f"【{title}】{question}")
        choices = _extract_choice_hints(question)
        if choices:
            lines.append("可选：" + " / ".join(humanize_ir_hitl_text(c) for c in choices))
    text = "\n".join(lines)
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def render_review_summary(
    field_results: list[dict] | None,
    *,
    max_chars: int = 2500,
) -> str:
    """Human-readable confirmed-IR digest for pre-SQL caliber review (no policy rules)."""
    body = render_context(field_results or [])
    if not body.strip():
        return ""
    # Drop the machine-oriented header; review pending supplies its own framing.
    lines = [line for line in body.splitlines() if line.strip()]
    if lines and lines[0].startswith("【DataTaskIR"):
        lines = lines[1:]
    if lines and "未出现的内容不构成约束" in lines[0]:
        lines = lines[1:]
    # Swap internal field titles for user-facing HITL titles.
    remapped: list[str] = []
    for line in lines:
        updated = line
        for field_id, internal_title in FIELD_TITLES.items():
            user_title = IR_HITL_USER_TITLES.get(field_id)
            if user_title and internal_title in updated:
                updated = updated.replace(internal_title, user_title)
        remapped.append(humanize_ir_hitl_text(updated))
    text = "\n".join(remapped).strip()
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def _field_map(field_results: list[dict] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in field_results or []:
        if not isinstance(item, dict):
            continue
        field_id = str(item.get("field_id") or "").strip()
        if field_id:
            out[field_id] = item
    return out


def _field_value_dict(field: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(field, dict):
        return {}
    value = field.get("value")
    return value if isinstance(value, dict) else {}


def render_caliber_review_structured_summary(
    field_results: list[dict] | None,
) -> tuple[str, list[str]]:
    """Business-facing 5-line digest + optional risk tips for caliber review.

    Returns ``(structured_text, risk_tips)``. Empty structured_text means nothing
    confirmed enough to ask.
    """
    by_id = _field_map(field_results)
    if not by_id:
        return "", []

    identity = _field_value_dict(by_id.get("identity"))
    components = [
        c for c in (identity.get("components") or []) if isinstance(c, dict) and c.get("field_ref")
    ]
    pk_bits: list[str] = []
    for comp in components:
        ref = str(comp.get("field_ref")).strip()
        role = str(comp.get("semantic_role") or "").strip()
        pk_bits.append(f"{role}（{ref}）" if role else ref)
    if pk_bits:
        grain_line = (
            f"- 主键/粒度：{' × '.join(pk_bits)}（一行对应一个这样的组合，共 {len(pk_bits)} 维）"
        )
    else:
        grain_line = "- 主键/粒度：摘要里尚未写清一行代表什么"

    of = _field_value_dict(by_id.get("output_features"))
    layout = of.get("layout")
    columns = expand_output_features(of) if of else []
    measure_n = len([m for m in (of.get("measures") or []) if isinstance(m, dict)] if of else [])
    feature_n = len([f for f in (of.get("features") or []) if isinstance(f, dict)] if of else [])
    axis_n = 0
    axis_bits: list[str] = []
    for axis in of.get("axes") or [] if of else []:
        if not isinstance(axis, dict):
            continue
        vals = [v for v in (axis.get("values") or []) if v is not None and str(v).strip()]
        if not vals:
            continue
        axis_n += 1
        axis_id = str(axis.get("axis_id") or "轴")
        axis_bits.append(f"{axis_id}×{len(vals)}")
    biz_cols = len(columns)
    total_with_pk = biz_cols + len(pk_bits)
    if layout == "cartesian" and (axis_bits or measure_n):
        shape = (
            f"多轴交叉展开（{', '.join(axis_bits) or '轴未写清'}）"
            f"×约 {measure_n or '若干'} 类指标，约 {biz_cols} 个业务列"
        )
    elif layout == "flat":
        shape = f"平铺输出约 {biz_cols or feature_n} 个业务列"
    elif biz_cols:
        shape = f"约 {biz_cols} 个业务列"
    else:
        shape = "输出形态尚未写清"
    output_line = (
        f"- 输出形态：{shape}；不含分区时合计约 {total_with_pk} 列"
        f"（主键 {len(pk_bits)} + 业务 {biz_cols}）"
    )

    sources = _field_value_dict(by_id.get("sources"))
    filter_bits: list[str] = []
    for bucket_name, entries in (
        ("事实", sources.get("facts") or []),
        ("维度", sources.get("dimensions") or []),
    ):
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            for pred in entry.get("filters") or []:
                if not isinstance(pred, dict) or not pred.get("field_ref"):
                    continue
                op = pred.get("operator") or ""
                ops = pred.get("operands") or []
                op_txt = _OPERATOR_RENDER.get(str(op), op) if op else ""
                operand_txt = ",".join(str(x) for x in ops[:3]) if isinstance(ops, list) else ""
                bit = f"{pred.get('field_ref')} {op_txt} {operand_txt}".strip()
                filter_bits.append(bit)
                if len(filter_bits) >= 4:
                    break
            if len(filter_bits) >= 4:
                break
        if len(filter_bits) >= 4:
            break
    if filter_bits:
        more = "…" if len(filter_bits) >= 4 else ""
        filter_line = f"- 过滤：已写 {len(filter_bits)}{'+' if more else ''} 条（如 {'；'.join(filter_bits[:3])}{more}）"
    else:
        filter_line = "- 过滤：摘要中未见已确认过滤（未写出的条件默认不会进 SQL）"

    time_val = _field_value_dict(by_id.get("time_context"))
    time_txt = _render_time_context(time_val).strip("。") if time_val else ""
    filter_refs = {b.split(" ", 1)[0].lower() for b in filter_bits if b}
    # Also collect raw filter field_refs for hour/day partition detection.
    for bucket_name, entries in (
        ("facts", sources.get("facts") or []),
        ("dimensions", sources.get("dimensions") or []),
    ):
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            for pred in entry.get("filters") or []:
                if isinstance(pred, dict) and pred.get("field_ref"):
                    filter_refs.add(str(pred["field_ref"]).strip().lower())
    source_refs = []
    for entry in list(sources.get("facts") or []) + list(sources.get("dimensions") or []):
        if isinstance(entry, dict) and entry.get("source_ref"):
            source_refs.append(str(entry["source_ref"]))
    hourish_source = any(
        str(ref).rsplit(".", 1)[-1].lower().endswith(("_hm", "_hh")) or "pt_h" in str(ref).lower()
        for ref in source_refs
    ) or ("pt_h" in filter_refs)
    bucket = (
        time_val.get("output_bucket") if isinstance(time_val.get("output_bucket"), dict) else {}
    )
    grain = str(bucket.get("grain") or "").lower()
    if time_txt:
        time_line = f"- 时间：{humanize_ir_hitl_text(time_txt)}"
    else:
        time_line = "- 时间：摘要中未见已确认时间边界"
    custom_def = str(bucket.get("custom_definition") or "")
    rationale_bits = " ".join(
        str(part)
        for part in (
            custom_def,
            (by_id.get("time_context") or {}).get("rationale"),
            (by_id.get("sources") or {}).get("rationale"),
        )
        if part
    )
    all_day_hours = any(
        m in rationale_bits
        for m in ("当天全部小时", "全部小时聚合", "全天小时", "当天所有小时", "不按 pt_h")
    )
    if hourish_source or grain == "hour" or "pt_h" in filter_refs or all_day_hours:
        if all_day_hours and "pt_h" not in filter_refs:
            time_line += "；小时口径：已确认当天全部小时聚合（只按天分区，不按 pt_h 切片）"
        elif "pt_h" in filter_refs and grain == "hour":
            time_line += "；小时口径：已写 pt_h 单小时切片"
        elif "pt_h" in filter_refs:
            time_line += "；已见 pt_h 过滤（请确认是单小时而非当天全部小时）"
        elif grain == "hour":
            time_line += "；output 分桶为小时，请确认 sources 是否同时有 pt_d+pt_h"
        else:
            time_line += "；源表像小时分区，但摘要未见 pt_h/单小时或「当天全部小时」确认"

    seq = _field_value_dict(by_id.get("sequence"))
    seq_bits: list[str] = []
    if seq:
        ordering = seq.get("ordering") or seq.get("order")
        if ordering:
            seq_bits.append(f"排序={humanize_ir_hitl_text(str(ordering)[:80])}")
        dedup = seq.get("dedup")
        if dedup:
            seq_bits.append("含序列去重")
        top_n = seq.get("top_n") or seq.get("limit")
        if top_n is not None:
            seq_bits.append(f"截断={top_n}")
    # Also peek measure intents for dedup/order cues when sequence empty.
    if not seq_bits and of:
        for item in list(of.get("measures") or [])[:2] + list(of.get("features") or [])[:2]:
            if not isinstance(item, dict):
                continue
            calc = item.get("calc") if isinstance(item.get("calc"), dict) else {}
            intent = str(calc.get("intent") or "")
            if any(k in intent for k in ("去重", "最近", "排序", "拼接", "序列")):
                seq_bits.append(humanize_ir_hitl_text(intent[:60]))
                break
    if seq_bits:
        dedup_line = f"- 去重与排序：{'；'.join(seq_bits)}"
    else:
        dedup_line = "- 去重与排序：摘要中未写清（同对象是否只留最近一次、由近到远等）"

    structured = "\n".join([grain_line, output_line, filter_line, time_line, dedup_line])
    risks: list[str] = []
    if biz_cols >= 12:
        risks.append(
            f"当前摘要业务列较多（约 {biz_cols} 列），请优先核对是否把本该合并的属性拆得过细，"
            "或把时段/窗口做成了过多交叉列。"
        )
    if layout == "cartesian" and axis_n >= 1 and measure_n >= 3 and biz_cols >= 12:
        risks.append(
            "交叉展开后列数与「主键 + 少量序列列」的常见形态差较多，请对照原话确认输出怎么摆。"
        )
    if hourish_source and "pt_h" not in filter_refs and grain != "hour" and not all_day_hours:
        risks.append(
            "源表像小时分区表，但摘要未见小时过滤/单小时确认，也未写明「当天全部小时聚合」；"
            "请确认是「只取指定小时」还是「当天全部小时合在一起」，避免无说明地只写天分区。"
        )
    return structured, risks


def format_ir_caliber_review_pending(
    field_results: list[dict] | None,
    *,
    max_chars: int = 3200,
    best_effort: bool = False,
) -> str:
    """Build HITL pending_action for the one-shot pre-SQL IR caliber review.

    Framing prioritizes scannable checklist + structured business summary so
    humans (and auto-resolve) spot grain/output-shape gaps before confirming.
    """
    structured, risks = render_caliber_review_structured_summary(field_results)
    detail = render_review_summary(field_results, max_chars=min(900, max(400, max_chars // 3)))
    if not structured and not detail:
        return ""

    lines = [
        f"{IR_CALIBER_REVIEW_USER_MARKER}（生成 SQL 前）",
        "请先按下面几点看【已确认口径】是否和你的需求一致；有不符先指出要改成什么，不要先确认。",
        "请留意：",
        "1) 一行代表什么？（仅用户 / 用户×时段桶 / 其它）",
        "2) 输出怎么摆？（时段或窗口做行维还是多列宽表；多属性是多列序列还是合成一条）",
        "3) 大概几列业务输出？（不含分区）",
        "4) 必留的过滤与时间边界有没有写进摘要？（含天分区；若源表是小时表，还要有小时分区/单小时还是全天小时）",
        "5) 去重与排序（同对象是否只留最近一次、由近到远）有没有写清？",
        "未在摘要中出现的过滤/去重/窗口等，默认不会写入 SQL。",
    ]
    if best_effort:
        lines.append(
            "说明：尚有未确认项已从约束中省略；下列只含已确认部分，已确认内容也可能和原话不一致，请对照核对点。"
        )
    if risks:
        lines.append("")
        lines.append("【请优先留意】")
        for tip in risks[:2]:
            lines.append(f"- {tip}")

    lines.append("")
    lines.append("【已确认口径 · 结构化摘要】")
    if structured:
        lines.append(structured)
    else:
        lines.append("- （结构化摘要不足，见下方细节）")

    if detail:
        lines.append("")
        lines.append("【补充细节】")
        lines.append(detail)

    omitted_notes: list[str] = []
    for output in field_results or []:
        if not isinstance(output, dict):
            continue
        field_id = str(output.get("field_id") or "").strip()
        unresolved = _normalize_unresolved(output.get("unresolved"))
        if not unresolved:
            continue
        title = IR_HITL_USER_TITLES.get(
            field_id, FIELD_TITLES.get(field_id, field_id or "未命名项")
        )
        # Prefer a short unresolved phrase over a generic "尚有未确认点".
        hint = humanize_ir_hitl_text(str(unresolved[0]))[:48] if unresolved else ""
        if hint:
            omitted_notes.append(f"- {title}：{hint}（未写入约束，定不了就不会进 SQL）")
        else:
            omitted_notes.append(f"- {title}：尚有未确认点（已省略，定不了就不会进 SQL）")
        if len(omitted_notes) >= 3:
            break
    if omitted_notes:
        lines.append("")
        lines.append("【未纳入约束】")
        lines.extend(omitted_notes)

    lines.append("")
    lines.append("回答方式：")
    lines.append(
        "- 有遗漏或错误：先用自然语言写清要补/要改的点（优先说一行代表什么、输出怎么摆、过滤与去重）"
    )
    lines.append("- 以上核对点全部无问题：再回复「确认，按此生成」")

    text = "\n".join(lines)
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


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


def _confirmed_identity_refs(by_id: dict[str, dict[str, Any]]) -> list[str]:
    output = by_id.get("identity")
    if output is None:
        return []
    raw = output.get("value", {})
    value = raw if isinstance(raw, dict) else {}
    refs: list[str] = []
    for item in value.get("components", []) or []:
        if not isinstance(item, dict):
            continue
        field_ref = item.get("field_ref")
        if field_ref is not None and str(field_ref).strip():
            refs.append(str(field_ref))
    return refs


def _confirmed_expanded_field_names(by_id: dict[str, dict[str, Any]]) -> list[str]:
    output = by_id.get("output_features")
    if output is None:
        return []
    raw = output.get("value", {})
    value = raw if isinstance(raw, dict) else {}
    names: list[str] = []
    for item in expand_output_features(value):
        name = item.get("field_name")
        if name is not None and str(name).strip():
            names.append(str(name))
    return names


def _render_final_output_column_contract(by_id: dict[str, dict[str, Any]]) -> str:
    key_refs = _confirmed_identity_refs(by_id)
    field_names = _confirmed_expanded_field_names(by_id)
    if not key_refs and not field_names:
        return ""
    key_aliases = {ref.split(".")[-1] for ref in key_refs}
    key_aliases.update(key_refs)
    business_names = [
        name
        for name in field_names
        if name not in key_aliases and name.split(".")[-1] not in key_aliases
    ]
    parts: list[str] = []
    if key_refs:
        parts.append(
            f"先按顺序输出特征主键列 [{', '.join(key_refs)}]"
            "（即使未出现在输出特征目录中也必须输出，不得遗漏或后置到指标列之后）"
        )
    else:
        parts.append("本任务暂无已确认特征主键前缀列")
    if business_names:
        parts.append(
            f"再覆盖业务/指标语义共 {len(business_names)} 列（IR 参考名：[{', '.join(business_names)}]）"
            "——由 expand(output_features) 确定性展开，cartesian 交叉语义不得合并或漏项；"
            "参考列名仅供主 Agent 对照，SQL/DDL 不强制使用同名"
        )
    elif field_names:
        parts.append("输出特征中的列均已由特征主键覆盖，不再额外增加业务/指标列")
    else:
        parts.append("本任务暂无已确认业务/指标输出列")
    total = len(key_refs) + len(business_names)
    parts.append(
        f"当前已记录的输出语义项共 {total}（不含分区列）；"
        "此数只用于核对已确认部分，不能视为信息不完备时的任务总列数；"
        "列名是否与 IR 参考名一致不作为约束"
    )
    return "；".join(parts) + "。"


def _render_per_field_aggregation_grain_contract(by_id: dict[str, dict[str, Any]]) -> str:
    output = by_id.get("output_features")
    if output is None:
        return ""
    raw = output.get("value", {})
    value = raw if isinstance(raw, dict) else {}
    grains: list[tuple[str, list[str] | None]] = []
    for item in expand_output_features(value):
        name = item.get("field_name")
        if name is None or not str(name).strip():
            continue
        aggregation = item.get("aggregation")
        if not isinstance(aggregation, dict):
            continue
        grain = aggregation.get("grain")
        if grain is None:
            continue
        if isinstance(grain, list):
            grains.append((str(name), [str(g) for g in grain if str(g).strip()]))
        else:
            grains.append((str(name), None))
    if not grains:
        return ""
    unique = {tuple(g or []) for _, g in grains if g is not None}
    parts = [
        f"{name}=[{', '.join(grain)}]" if grain is not None else f"{name}=未确认"
        for name, grain in grains
        if grain is not None
    ]
    if not parts:
        return ""
    summary = "；".join(parts)
    if len(unique) > 1:
        summary += (
            "。已确认存在多种聚合粒度：各输出列必须按其自身 aggregation.grain 聚合，"
            "禁止用单一全局 GROUP BY 覆盖全部特征"
        )
    else:
        summary += "。各输出列须按其标注的聚合粒度执行 GROUP BY"
    return summary + "。"


def _render_identity(value: dict[str, Any]) -> str:
    parts = []
    for item in value.get("components", []) or []:
        if not isinstance(item, dict):
            continue
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
        rendered.append(
            "上述主键列是最终输出的必选前缀列，即使未写入输出特征目录也必须出现在 INSERT SELECT 中"
        )
    if value.get("uniqueness_scope") is not None:
        rendered.append(f"唯一范围={value.get('uniqueness_scope')}")
    return "；".join(rendered) + ("。" if rendered else "")


def _render_filter_scope(item: dict[str, Any]) -> str | None:
    filter_scope = item.get("filter_scope")
    if filter_scope == "window_branch":
        branch = item.get("branch_ref")
        return f"仅{branch}分支" if branch else "仅所属窗口分支"
    if filter_scope == "source_level":
        return "整个事实源"
    return None


def _render_predicate(item: dict[str, Any], source_ref: Any) -> str:
    source = f"{source_ref}." if source_ref is not None else ""
    operator = item.get("operator")
    operator_text = _OPERATOR_RENDER.get(str(operator), operator) if operator is not None else None
    base = " ".join(
        str(value)
        for value in [
            f"{source}{item.get('field_ref')}" if item.get("field_ref") is not None else None,
            operator_text,
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


def _render_dedup_block(dedup: Any, label: str) -> str:
    if not isinstance(dedup, dict):
        return ""
    identity = dedup.get("record_identity") or {}
    selection = dedup.get("record_selection") or {}
    identity_fields = identity.get("fields", []) or [] if isinstance(identity, dict) else []
    criteria = selection.get("criteria") if isinstance(selection, dict) else None
    if not identity_fields and not criteria:
        return ""
    parts = [label]
    identity_text = _attributes(
        [
            ("同一事实字段", identity_fields),
            ("空值等价", identity.get("null_equality") if isinstance(identity, dict) else None),
        ]
    )
    if identity_text:
        parts.append(identity_text)
    if (
        identity_fields
        and not criteria
        and (not isinstance(selection, dict) or selection.get("if_all_criteria_equal") is None)
    ):
        parts.append("未定义选择函数，不产生去重")
    elif isinstance(selection, dict):
        selection_text = _render_selection(selection)
        if selection_text:
            parts.append(selection_text)
    return "；".join(parts)


def _render_sources(value: dict[str, Any]) -> str:
    parts: list[str] = []
    for fact in value.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        source = fact.get("source_ref")
        fact_parts = [f"事实源={source}" if source is not None else "事实源"]
        predicates = [
            _render_predicate(item, item.get("source_ref") or source)
            for item in (fact.get("filters") or [])
            if isinstance(item, dict)
        ]
        if predicates:
            fact_parts.append("过滤=" + "且".join(p for p in predicates if p))
        derived = [
            _render_derived(item, source)
            for item in (fact.get("derived") or [])
            if isinstance(item, dict)
        ]
        if derived:
            fact_parts.append("派生=" + "；".join(d for d in derived if d))
        dedup_text = _render_dedup_block(fact.get("dedup"), "事实去重")
        if dedup_text:
            fact_parts.append(dedup_text)
        parts.append("，".join(fact_parts))

    for dim in value.get("dimensions") or []:
        if not isinstance(dim, dict):
            continue
        source = dim.get("source_ref")
        dim_parts = [f"维度源={source}" if source is not None else "维度源"]
        predicates = [
            _render_predicate(item, item.get("source_ref") or source)
            for item in (dim.get("filters") or [])
            if isinstance(item, dict)
        ]
        if predicates:
            dim_parts.append("过滤=" + "且".join(p for p in predicates if p))
        derived = [
            _render_derived(item, source)
            for item in (dim.get("derived") or [])
            if isinstance(item, dict)
        ]
        if derived:
            dim_parts.append("派生=" + "；".join(d for d in derived if d))
        dedup_text = _render_dedup_block(dim.get("dedup"), "维度去重")
        if dedup_text:
            dim_parts.append(dedup_text)
        join = dim.get("join")
        if isinstance(join, dict) and join:
            join_bits = [
                join.get("fact_source_ref"),
                join.get("join_type"),
                "join",
                source,
            ]
            join_text = " ".join(str(b) for b in join_bits if b is not None)
            conditions = []
            for item in join.get("conditions") or []:
                if not isinstance(item, dict):
                    continue
                base = " ".join(
                    str(child)
                    for child in [
                        item.get("fact_field"),
                        item.get("operator") or "eq",
                        item.get("dim_field") or item.get("dimension_field"),
                    ]
                    if child is not None
                )
                if base:
                    conditions.append(base)
            if conditions:
                join_text += f" on {' 且 '.join(conditions)}"
            attrs = _attributes(
                [
                    ("基数", join.get("cardinality")),
                    ("未匹配事实", join.get("unmatched_fact")),
                    ("时间有效条件", join.get("temporal_validity")),
                ]
            )
            if attrs:
                join_text += f"（{attrs}）"
            dim_parts.append(join_text)
        parts.append("，".join(dim_parts))

    if not parts:
        return "无已确认数据源预处理。"
    return "；".join(parts) + "。"


def _render_time_context(value: dict[str, Any]) -> str:
    rendered: list[str] = []
    bucket = value.get("output_bucket") or {}
    if isinstance(bucket, dict):
        bucket_parts = []
        if bucket.get("time_field") is not None:
            bucket_parts.append(f"输出分桶字段={bucket.get('time_field')}")
        attrs = _attributes(
            [
                ("粒度", bucket.get("grain")),
                ("时区", bucket.get("timezone")),
                ("日历", bucket.get("calendar")),
                ("周起始", bucket.get("week_start")),
                ("自定义定义", bucket.get("custom_definition")),
            ]
        )
        if attrs:
            bucket_parts.append(attrs)
        if bucket_parts:
            rendered.append("输出分桶：" + "；".join(bucket_parts))
    windows = value.get("input_windows") or []
    window_texts = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        wid = window.get("window_id")
        parts = [f"窗口={wid}" if wid is not None else "窗口"]
        if window.get("time_field") is not None:
            parts.append(f"时间字段={window.get('time_field')}")
        if window.get("anchor") is not None:
            parts.append(f"锚点={window.get('anchor')}")
        range_value = window.get("range") or {}
        if isinstance(range_value, dict):
            range_parts = [
                range_value.get("direction"),
                range_value.get("number"),
                range_value.get("unit"),
            ]
            range_text = " ".join(str(item) for item in range_parts if item is not None)
            if range_text:
                parts.append(f"范围={range_text}")
            range_attrs = _attributes(
                [
                    ("范围类型", range_value.get("type")),
                    ("包含锚点周期", range_value.get("include_anchor_period")),
                ]
            )
            if range_attrs:
                parts.append(range_attrs)
        if window.get("boundary") is not None:
            parts.append(f"边界={window.get('boundary')}")
        window_texts.append("，".join(parts))
    if window_texts:
        rendered.append("输入回看窗口：" + "；".join(window_texts))
    return "；".join(rendered) + ("。" if rendered else "")


def _render_output_features(value: dict[str, Any]) -> str:
    layout = value.get("layout")
    columns = expand_output_features(value)
    if not columns and layout not in ("cartesian", "flat"):
        return "无已确认输出特征。"
    header_parts = [f"layout={layout}" if layout else "layout=未确认"]
    if layout == "cartesian":
        axes = value.get("axes") or []
        measures = value.get("measures") or []
        axis_desc = []
        for axis in axes:
            if isinstance(axis, dict):
                axis_desc.append(f"{axis.get('axis_id')}×{len(axis.get('values') or [])}")
        measure_ids = [
            str(m.get("measure_id"))
            for m in measures
            if isinstance(m, dict) and m.get("measure_id")
        ]
        if axis_desc:
            header_parts.append(f"交叉声明=[{', '.join(axis_desc)}]")
        if measure_ids:
            header_parts.append(f"指标模板=[{', '.join(measure_ids)}]")
        extras = value.get("extras") or []
        if extras:
            header_parts.append(f"extras={len(extras)}")
    header_parts.append(
        f"当前已展开 {len(columns)} 个业务/指标列（未展开的未知项不代表不需要输出）"
    )
    prefix = (
        "，".join(header_parts) + "（cartesian 由代码确定性展开，禁止少列/合并；"
        "各列聚合粒度以 aggregation.grain 为准；calc.intent 为语义约束，不是必须照抄的 SQL；"
        "展开列名仅供主 Agent 参考，SQL 不强制同名）"
    )
    if not columns:
        return prefix + "。"
    rendered = []
    for item in columns:
        calc = item.get("calc") or {}
        intent = calc.get("intent") if isinstance(calc, dict) else None
        source = item.get("source")
        if intent:
            origin_text = f"计算口径说明={intent}"
            if source:
                origin_text += f"（主要来源={source}）"
        else:
            origin_text = f"来源={source or '未确认'}"
        aggregation = item.get("aggregation")
        grain_text = ""
        if isinstance(aggregation, dict):
            kind = aggregation.get("kind")
            if kind and kind not in (None, "null"):
                kind_label = _AGG_KIND_RENDER.get(str(kind), str(kind))
                grain_text += f"，聚合方式={kind_label}"
            if "grain" in aggregation:
                grain = aggregation.get("grain")
                if isinstance(grain, list):
                    grain_text += f"，聚合粒度=[{', '.join(str(g) for g in grain)}]"
                elif grain is not None:
                    grain_text += f"，聚合粒度={grain}"
        window_ref = item.get("window_ref")
        window_text = f"，窗口={window_ref}" if window_ref else ""
        rank = item.get("rank")
        rank_text = ""
        if isinstance(rank, dict) and rank:
            rank_text = (
                f"，排名={rank.get('rank_type')}"
                f"(桶={rank.get('bucket_count')}，方向={rank.get('order_direction')}，"
                f"基于={rank.get('based_on') or rank.get('order_by')})"
            )
        rendered.append(
            f"{item.get('field_name')}: {origin_text}，类型={item.get('output_type')}"
            f"{grain_text}{window_text}{rank_text}，说明={item.get('description')}"
        )
    return prefix + "：" + "；".join(rendered) + "。"


def _render_pipeline(value: dict[str, Any]) -> str:
    parts: list[str] = []
    dedup_before_join = value.get("deduplication_before_join")
    if dedup_before_join is not None:
        parts.append(
            "事实去重作用于事实记录，必须先于维度关联（JOIN）执行，JOIN 扇出产生的行必须全部保留，"
            "禁止 JOIN 之后再按事实去重键去重/分组折叠"
            if dedup_before_join
            else "证据明确去重作用于 JOIN 后的富化行（先 JOIN 再去重），去重键必须覆盖扇出区分维度列"
        )
    dedup_before_agg = value.get("deduplication_before_aggregation")
    if dedup_before_agg is not None:
        parts.append("去重先于聚合" if dedup_before_agg else "聚合基于原始记录（不去重）")
    operation_order = value.get("operation_order") or []
    if operation_order:
        parts.append(
            f"执行管道顺序={operation_order}（按此先后执行，禁止调整顺序或改变操作的作用层级）"
        )
    return "；".join(parts) + "。" if parts else ""


def _render_sequence(value: dict[str, Any]) -> str:
    parts: list[str] = []
    fields = value.get("partition_fields") or []
    if fields:
        parts.append(f"逻辑序列分区={fields}")
        if value.get("null_equality") is not None:
            parts.append(f"空值等价={value.get('null_equality')}")
    ordering = value.get("ordering") or []
    if ordering:
        order_parts = []
        for item in ordering:
            if not isinstance(item, dict):
                continue
            base = " ".join(
                str(child)
                for child in [item.get("field_ref"), item.get("direction")]
                if child is not None
            )
            attributes = _attributes(
                [
                    ("类型", item.get("value_type")),
                    ("空值位置", item.get("null_rank")),
                ]
            )
            order_parts.append(f"{base}（{attributes}）" if attributes else base)
        if order_parts:
            parts.append(f"排序=按 {'，再按 '.join(order_parts)}")
    dedup = value.get("dedup")
    if isinstance(dedup, dict) and dedup.get("on_duplicate") is not None:
        identity = dedup.get("identity_ref")
        if identity == "custom":
            identity = f"custom:{dedup.get('custom_identity_fields', [])}"
        elif identity == "feature_key":
            identity = "identity"
        dedup_parts = []
        if identity is not None:
            dedup_parts.append(f"重复身份={identity}")
        dedup_parts.append(f"重复行为={dedup.get('on_duplicate')}")
        selection = _render_selection(dedup.get("record_selection") or {})
        if selection:
            dedup_parts.append(selection)
        parts.append("序列去重=" + "；".join(dedup_parts))
    truncation = value.get("truncation")
    if isinstance(truncation, dict) and truncation.get("limit_per_partition") is not None:
        limit = truncation.get("limit_per_partition")
        tie_behavior = truncation.get("tie_behavior")
        tie_text = ""
        if tie_behavior == "retain_all":
            tie_text = "；并列时全部保留"
        elif tie_behavior == "truncate_exact":
            tie_text = "；并列时仍只取上限条数"
        parts.append(f"截断=每个逻辑序列保留前 {limit} 条{tie_text}")
    window_partition = value.get("window_partition")
    if isinstance(window_partition, dict):
        wp_fields = window_partition.get("partition_fields")
        if wp_fields:
            parts.append(f"独立窗口分区键={wp_fields}")
    if not parts:
        return "无最终序列后处理（不适用）。"
    return "；".join(parts) + "。"


def _render_json_value(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_RENDERERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "identity": _render_identity,
    "sources": _render_sources,
    "time_context": _render_time_context,
    "output_features": _render_output_features,
    "pipeline": _render_pipeline,
    "sequence": _render_sequence,
}
