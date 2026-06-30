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
"""Agent 级 pre-hook：上下文指代查询改写。

在 Planner 运行前，将用户 query 中的模糊指代（如「刚才那个表」）改写为轨迹中
具体节点引用；改写结果写回 ``state["user_query"]`` 并同步 ``QueryNode.query``。
原始输入保留在 ``state["raw_user_query"]`` 与 ``QueryNode.raw_user_query``。

三阶段流程：
- Stage A：仅根据 query 判断是否需要改写及指代类型/时间语义（不调候选、不调改写 LLM）。
- Stage B：按 Stage A 意图从 trajectory 筛选小候选集。
- Stage C：改写 LLM + 校验 + 写回。

仅主 Agent（``sub_id == 0``）执行；Subagent 直接 return，不改写、不调 LLM。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.messages import HumanMessage
from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.context.context import Context
from dataagent.core.flex.hooks.agent_turn import is_subagent
from dataagent.core.flex.utils.context_from_state import get_context_for_flex_state
from dataagent.core.flex.workflow.state import FlexState
from dataagent.utils.parsing_utils import extract_json_block

DEFAULT_MAX_CANDIDATES = 100

CANDIDATE_NODE_TYPES = frozenset({"Table", "File", "Script", "Action", "Tool", "Response", "State"})

VALID_TEMPORAL_HINTS = frozenset({"recent", "earliest", "latest", "specific"})

DEFAULT_TEMPORAL_HINT = "recent"

_CONTEXT_TZ = timezone(timedelta(hours=8))
_MIN_CREATED_AT = datetime.min.replace(tzinfo=_CONTEXT_TZ)

SUMMARY_MAX_CHARS = 200

ANALYZE_PROMPT_TEMPLATE = """你是数据分析对话中的用户问题意图分析助手。仅根据用户问题文本判断是否存在需要查询轨迹上下文的指代，不涉及具体轨迹节点。

任务：
1. 判断用户问题是否引用前文中的数据产物、操作或结论（如「刚才那个表」「第一个生成的文件」「上一步结果」）。
2. 若是直接请求新操作、无上下文依赖的问题，needs_rewrite=false。
3. 若存在指代，列出每个指代表达及其可能的目标类型与时间语义。

指代目标类型（target_types）只能从以下选取一个或多个：Table, File, Script, Action, Tool, Response, State。
时间语义（temporal_hint）：
- recent：刚才、上一个、最近、刚生成的
- earliest：第一个、最早、最初
- latest：最新、最后
- specific：明确点名或路径，或不强调时间顺序

输出必须是合法 JSON，不要输出 markdown 代码块外的解释：

{{
  "needs_rewrite": true | false,
  "mentions": [
    {{
      "text": "指代表达原文",
      "target_types": ["Table"],
      "temporal_hint": "recent" | "earliest" | "latest" | "specific"
    }}
  ],
  "skip_reason": "needs_rewrite=false 时说明原因，true 时为空字符串"
}}

示例（无指代）：
用户问题：帮我生成 test2.txt，内容为 test2
输出：
{{
  "needs_rewrite": false,
  "mentions": [],
  "skip_reason": "直接请求新操作，无上下文指代"
}}

示例（有指代）：
用户问题：第一个生成的文件里有什么内容？
输出：
{{
  "needs_rewrite": true,
  "mentions": [
    {{"text": "第一个生成的文件", "target_types": ["File"], "temporal_hint": "earliest"}}
  ],
  "skip_reason": ""
}}

用户问题：
{raw_query}
"""

_CANDIDATE_ORDER_HINT_RECENT = """候选数组已按时间由近到远排列（run_id、created_at 降序）：
- 「刚才」「上一个」「最近」通常指数组中更靠前（更近）的候选。
- 「第一个生成」「最早生成」指时间更早的候选，在数组中更靠后；不要误选数组第一个。"""

_CANDIDATE_ORDER_HINT_EARLIEST = """候选数组已按时间由远到近排列（run_id、created_at 升序）：
- 「第一个生成」「最早生成」通常指数组中更靠前（更早）的候选。
- 「刚才」「最近」指时间更近的候选，在数组中更靠后；不要误选数组末尾。"""

PROMPT_TEMPLATE = """你是数据分析对话中的上下文指代查询助手。用户问题可能引用前文中出现过的数据产物、操作或结论。

任务：
1. 判断用户问题是否包含需要查询的指代（如「刚才那个表」「上一步的结果」「你之前说的」等）。
2. 若存在指代且能从候选列表中**唯一**确定目标，输出 decision="rewrite" 并给出改写后的问题。
3. 若无指代、目标不明确、多个候选无法区分、或候选不足，输出 decision="skip"。

{candidate_order_hint}

硬性约束：
- target_node 必须且只能来自下方候选列表中的 node_id，禁止编造节点。
- node_id 格式为 Type(label)，必须与候选 JSON 里 node_id 字段完全一致，例如 File(file00000)、Table(table00003)。
- 禁止只写 label（错误示例：file00000）；必须抄写完整 node_id（正确示例：File(file00000)）。
- decision 只能是 "rewrite" 或 "skip"。
- decision="rewrite" 时 rewrite_query 非空，resolved_refs 非空，每项含 mention、target_node、reason。
- decision="skip" 时填写 skip_reason，rewrite_query 可为空字符串。
- 同一 mention 只能对应一个 target_node；多个指代各自唯一时可同时替换。
- 不确定时务必 skip，宁可漏改不要错改。

输出必须是合法 JSON，不要输出 markdown 代码块外的解释：

{{
  "decision": "rewrite" | "skip",
  "rewrite_query": "改写后的完整用户问题",
  "resolved_refs": [
    {{"mention": "原指代表达", "target_node": "候选中的 node_id", "reason": "简短理由"}}
  ],
  "skip_reason": "skip 时说明原因，rewrite 时为空字符串"
}}

示例（rewrite，注意 target_node 为完整 node_id）：
用户问题：用刚才那个表继续分析
输出：
{{
  "decision": "rewrite",
  "rewrite_query": "用表 Table(table00003)，路径 /workspace/result.csv 继续分析",
  "resolved_refs": [
    {{"mention": "刚才那个表", "target_node": "Table(table00003)", "reason": "候选中最近的唯一 Table"}}
  ],
  "skip_reason": ""
}}

示例（rewrite，File 指代）：
用户问题：第一个生成的文件里有什么内容？
输出：
{{
  "decision": "rewrite",
  "rewrite_query": "File(file00000)，路径 /workspace/test1.txt 里有什么内容？",
  "resolved_refs": [
    {{"mention": "第一个生成的文件", "target_node": "File(file00000)", "reason": "按时间顺序第一个 File 节点"}}
  ],
  "skip_reason": ""
}}

示例（skip，无指代）：
用户问题：帮我生成 test2.txt，内容为 test2
输出：
{{
  "decision": "skip",
  "rewrite_query": "",
  "resolved_refs": [],
  "skip_reason": "直接请求新操作，无上下文指代"
}}

用户原始问题：
{raw_query}

候选对象（JSON 数组，{candidate_order_label}）：
{candidates_json}
"""


@dataclass(frozen=True)
class ReferenceMention:
    """Stage A 解析出的单个指代表达。"""

    text: str
    target_types: frozenset[str]
    temporal_hint: str


@dataclass(frozen=True)
class QueryAnalysis:
    """Stage A 查询意图分析结果。"""

    needs_rewrite: bool
    mentions: tuple[ReferenceMention, ...]
    skip_reason: str


@dataclass(frozen=True)
class RewritePlan:
    """校验通过的改写计划。"""

    rewrite_query: str
    resolved_refs: list[dict[str, Any]]
    target_nodes: list[str]


def _ensure_raw_user_query(state: FlexState) -> str:
    """初始化 ``raw_user_query``，不覆盖已有值。

    Args:
        state: Flex 工作流状态。

    Returns:
        本轮用户原始 query 字符串。
    """
    existing = state.get("raw_user_query")
    if existing is not None and str(existing).strip():
        return str(existing).strip()
    raw = str(state.get("user_query") or "").strip()
    if raw:
        state["raw_user_query"] = raw
    return raw


def _resolve_node_created_at(context: Context, node_id: str) -> datetime:
    """从 Context IR 读取节点创建时间，用于同 run 内候选排序。

    Args:
        context: 当前会话 Context。
        node_id: 轨迹图节点名，如 ``Table(table00001)``。

    Returns:
        节点 ``created_at``；无法解析时返回带时区的最小时间戳。
    """
    try:
        return context.get_IR_from_node(graph_node_label=node_id).created_at
    except (ValueError, KeyError):
        return _MIN_CREATED_AT


def _build_candidate_sort_key(
    *,
    run_id: int,
    created_at: datetime,
    node_id: str,
    use_earliest_order: bool,
) -> tuple[int, float, str]:
    """构造候选节点排序键：run_id、创建时间、node_id。

    Args:
        run_id: 节点所属 run。
        created_at: 节点 IR 创建时间。
        node_id: 轨迹图节点名，用作 tie-break。
        use_earliest_order: 为 True 时按 run/时间升序，否则按近优先降序。

    Returns:
        可用于 ``list.sort`` 的三元组排序键。
    """
    time_key = created_at.timestamp()
    if use_earliest_order:
        return run_id, time_key, node_id
    return -run_id, -time_key, node_id


def _truncate_text(value: Any, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """截断长文本用于候选摘要。

    Args:
        value: 任意可转字符串的值。
        max_chars: 最大字符数。

    Returns:
        截断后的字符串。
    """
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _extract_action_name(action_value: Any) -> str:
    """从 Action 节点的 action 字段提取工具/动作名。

    Args:
        action_value: 轨迹节点上的 action 属性，如 ``Tool(calculator)``。

    Returns:
        动作名；无法解析时返回空字符串。
    """
    text = str(action_value or "").strip()
    match = re.fullmatch(r"Tool\((.+)\)", text)
    return match.group(1).strip() if match else text


def _find_producer_action(trajectory: Any, node_id: str) -> tuple[str, str]:
    """查找产出该节点的上游 Action 节点。

    Args:
        trajectory: NetworkX 轨迹图。
        node_id: 图节点 label，如 ``Table(table00001)``。

    Returns:
        (from_action, action_name) 元组。
    """
    try:
        predecessors = list(trajectory.predecessors(node_id))
    except KeyError:
        return "", ""
    for pred in predecessors:
        if not str(pred).startswith("Action("):
            continue
        try:
            attrs = trajectory.nodes[pred]
        except KeyError:
            continue
        if attrs.get("node_type") == "Action":
            action_name = _extract_action_name(attrs.get("action"))
            return pred, action_name
    if predecessors:
        return str(predecessors[0]), ""
    return "", ""


def _build_candidate_entry(trajectory: Any, node_id: str, attrs: dict[str, Any]) -> dict[str, Any]:
    """将轨迹节点转为 LLM 候选 JSON 条目。

    Args:
        trajectory: NetworkX 轨迹图。
        node_id: 图节点 label。
        attrs: 节点属性字典。

    Returns:
        候选对象字典。
    """
    node_type = str(attrs.get("node_type") or "")
    from_action, action_name = _find_producer_action(trajectory, node_id)
    entry: dict[str, Any] = {
        "node_id": node_id,
        "node_type": node_type,
        "description": _truncate_text(attrs.get("description")),
        "run_id": attrs.get("run_id"),
    }
    if from_action:
        entry["from_action"] = from_action
    if action_name:
        entry["action_name"] = action_name

    if node_type in {"Table", "File"}:
        entry["path"] = _truncate_text(attrs.get("path"))
    elif node_type == "Script":
        entry["script_type"] = _truncate_text(attrs.get("script_type"))
        entry["summary"] = _truncate_text(attrs.get("script_content"))
    elif node_type == "Action":
        entry["action_name"] = _extract_action_name(attrs.get("action"))
        entry["params_summary"] = _truncate_text(attrs.get("params"))
        entry["success"] = attrs.get("success")
    elif node_type == "Tool":
        entry["params_summary"] = _truncate_text(attrs.get("tool_params"))
        entry["returns_summary"] = _truncate_text(attrs.get("tool_returns"))
    elif node_type == "Response":
        entry["summary"] = _truncate_text(attrs.get("response"))
    elif node_type == "State":
        entry["summary"] = _truncate_text(attrs.get("current_status") or attrs.get("content"))

    return entry


def _normalize_target_types(raw_types: Any) -> frozenset[str]:
    """将 Stage A 输出的 target_types 归一化为合法节点类型集合。

    Args:
        raw_types: LLM 输出的 target_types 字段。

    Returns:
        与 ``CANDIDATE_NODE_TYPES`` 交集后的类型集合；空时返回全部候选类型。
    """
    if not isinstance(raw_types, list):
        return CANDIDATE_NODE_TYPES
    normalized = {str(item).strip() for item in raw_types if str(item).strip() in CANDIDATE_NODE_TYPES}
    return frozenset(normalized) if normalized else CANDIDATE_NODE_TYPES


def _normalize_temporal_hint(raw_hint: Any) -> str:
    """将 Stage A 输出的 temporal_hint 归一化为合法枚举值。

    Args:
        raw_hint: LLM 输出的 temporal_hint 字段。

    Returns:
        合法 temporal_hint；非法时返回 ``DEFAULT_TEMPORAL_HINT``。
    """
    hint = str(raw_hint or "").strip().lower()
    if hint in VALID_TEMPORAL_HINTS:
        return hint
    return DEFAULT_TEMPORAL_HINT


def _merge_analysis_filters(mentions: tuple[ReferenceMention, ...]) -> tuple[frozenset[str], str]:
    """合并多个指代的类型过滤与时间排序偏好。

    Args:
        mentions: Stage A 解析出的指代列表。

    Returns:
        (target_types, temporal_hint) 元组。
    """
    if not mentions:
        return CANDIDATE_NODE_TYPES, DEFAULT_TEMPORAL_HINT

    merged_types: set[str] = set()
    for mention in mentions:
        merged_types.update(mention.target_types)
    target_types = frozenset(merged_types) if merged_types else CANDIDATE_NODE_TYPES

    hints = {mention.temporal_hint for mention in mentions}
    if "earliest" in hints:
        temporal_hint = "earliest"
    elif "latest" in hints or "recent" in hints:
        temporal_hint = "recent"
    else:
        temporal_hint = DEFAULT_TEMPORAL_HINT

    return target_types, temporal_hint


def _collect_candidates(
    context: Context,
    max_candidates: int,
    *,
    target_types: frozenset[str] | None = None,
    temporal_hint: str = DEFAULT_TEMPORAL_HINT,
) -> list[dict[str, Any]]:
    """从轨迹 DAG 按意图筛选候选节点并排序截断。

    Args:
        context: 当前会话 Context。
        max_candidates: 候选上限。
        target_types: 允许的节点类型；None 表示全部 ``CANDIDATE_NODE_TYPES``。
        temporal_hint: 时间语义，``earliest`` 时按 ``run_id``、``created_at`` 正序，
            其余按近优先；同 run 内以 ``node_id`` 稳定 tie-break。

    Returns:
        候选对象列表，长度不超过 ``max_candidates``。
    """
    allowed_types = target_types if target_types is not None else CANDIDATE_NODE_TYPES
    trajectory = context.get_trajectory(trimmed=False)
    raw_entries: list[tuple[tuple[int, float, str], dict[str, Any]]] = []
    use_earliest_order = temporal_hint == "earliest"

    for node_id, attrs in trajectory.nodes(data=True):
        node_type = str(attrs.get("node_type") or "")
        if node_type not in allowed_types:
            continue
        node_name = str(node_id)
        run_id = int(attrs.get("run_id") or 0)
        created_at = _resolve_node_created_at(context, node_name)
        sort_key = _build_candidate_sort_key(
            run_id=run_id,
            created_at=created_at,
            node_id=node_name,
            use_earliest_order=use_earliest_order,
        )
        entry = _build_candidate_entry(trajectory, node_name, dict(attrs))
        raw_entries.append((sort_key, entry))

    raw_entries.sort(key=lambda item: item[0])
    return [entry for _, entry in raw_entries[:max_candidates]]


def _build_analyze_prompt(raw_query: str) -> str:
    """组装 Stage A 意图分析 prompt。

    Args:
        raw_query: 用户原始问题。

    Returns:
        完整 prompt 字符串。
    """
    return ANALYZE_PROMPT_TEMPLATE.format(raw_query=raw_query)


def _build_llm_prompt(
    raw_query: str,
    candidates: list[dict[str, Any]],
    *,
    temporal_hint: str = DEFAULT_TEMPORAL_HINT,
) -> str:
    """组装 Stage C 改写 LLM 用户 prompt。

    Args:
        raw_query: 用户原始问题。
        candidates: 候选对象列表。
        temporal_hint: Stage B 使用的排序语义，与 ``_collect_candidates`` 一致。

    Returns:
        完整 prompt 字符串。
    """
    use_earliest_order = temporal_hint == "earliest"
    candidate_order_hint = _CANDIDATE_ORDER_HINT_EARLIEST if use_earliest_order else _CANDIDATE_ORDER_HINT_RECENT
    candidate_order_label = "按时间由远到近" if use_earliest_order else "按时间由近到远"
    candidates_json = json.dumps(candidates, ensure_ascii=False, indent=2)
    return PROMPT_TEMPLATE.format(
        raw_query=raw_query,
        candidate_order_hint=candidate_order_hint,
        candidate_order_label=candidate_order_label,
        candidates_json=candidates_json,
    )


def _invoke_llm(runtime: Runtime, prompt: str) -> str:
    """同步调用 LLM 并返回文本内容。

    Args:
        runtime: Flex 运行时。
        prompt: 用户 prompt。

    Returns:
        LLM 响应文本。

    Raises:
        Exception: LLM 调用失败时向上抛出。
    """
    llm = runtime.llm("context_reference_rewriter")
    response = llm.invoke([HumanMessage(content=prompt)])
    content = getattr(response, "content", response)
    return str(content or "").strip()


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    """解析 LLM 输出的 JSON 对象。

    Args:
        text: LLM 原始响应文本。

    Returns:
        解析后的字典；失败返回 None。
    """
    try:
        parsed = extract_json_block(text)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _parse_query_analysis(parsed: dict[str, Any]) -> QueryAnalysis | None:
    """解析 Stage A LLM 输出的查询意图分析结果。

    Args:
        parsed: LLM 解析后的 JSON 对象。

    Returns:
        ``QueryAnalysis``；解析失败返回 None。
    """
    needs_rewrite = parsed.get("needs_rewrite")
    if not isinstance(needs_rewrite, bool):
        return None

    skip_reason = str(parsed.get("skip_reason") or "").strip()
    raw_mentions = parsed.get("mentions")
    if not isinstance(raw_mentions, list):
        raw_mentions = []

    mentions: list[ReferenceMention] = []
    for item in raw_mentions:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        mentions.append(
            ReferenceMention(
                text=text,
                target_types=_normalize_target_types(item.get("target_types")),
                temporal_hint=_normalize_temporal_hint(item.get("temporal_hint")),
            )
        )

    return QueryAnalysis(
        needs_rewrite=needs_rewrite,
        mentions=tuple(mentions),
        skip_reason=skip_reason,
    )


def _analyze_query_intent(runtime: Runtime, raw_query: str) -> tuple[QueryAnalysis | None, str]:
    """Stage A：仅根据 query 分析是否需要指代消解。

    Args:
        runtime: Flex 运行时。
        raw_query: 用户原始 query。

    Returns:
        (analysis, skip_reason)：成功时 skip_reason 为空；失败时 analysis 为 None。
    """
    try:
        prompt = _build_analyze_prompt(raw_query)
        llm_text = _invoke_llm(runtime, prompt)
    except Exception as exc:
        return None, f"analyze_llm_failed:{type(exc).__name__}"

    parsed = _parse_llm_json(llm_text)
    if parsed is None:
        return None, "analyze_invalid_json"

    logger.debug(f"[context_reference_rewriter] analyze output: {parsed}")
    analysis = _parse_query_analysis(parsed)
    if analysis is None:
        return None, "analyze_invalid_schema"

    return analysis, ""


def _resolve_target_node(target_node: str, candidate_ids: set[str]) -> tuple[str | None, str]:
    """将 LLM 返回的 target_node 归一化为候选集合中的 node_id。

    若 LLM 只返回裸 label（如 ``file00000``），在候选中按括号内 label 唯一匹配时归一化
    为 ``File(file00000)``；多个候选同名 label 时拒绝。

    Args:
        target_node: LLM 输出的 target_node 字符串。
        candidate_ids: 合法候选 node_id 集合。

    Returns:
        (resolved_id, skip_reason)：成功时 resolved_id 为完整 node_id，skip_reason 为空；
        失败时 resolved_id 为 None。
    """
    raw = str(target_node or "").strip()
    if not raw:
        return None, "empty_target_node"
    if raw in candidate_ids:
        return raw, ""

    if re.fullmatch(r"\w+\(.+\)", raw):
        return None, f"target_not_in_candidates:{raw}"

    label_matches: list[str] = []
    for cid in candidate_ids:
        match = re.fullmatch(r"\w+\((.+)\)", cid)
        if match is not None and match.group(1) == raw:
            label_matches.append(cid)
    if len(label_matches) == 1:
        return label_matches[0], ""
    if len(label_matches) > 1:
        return None, f"ambiguous_target_label:{raw}"
    return None, f"target_not_in_candidates:{raw}"


def _validate_rewrite_plan(
    parsed: dict[str, Any],
    candidate_ids: set[str],
    raw_query: str,
) -> tuple[RewritePlan | None, str]:
    """校验 LLM 改写计划是否满足安全写回条件。

    Args:
        parsed: LLM 解析后的 JSON 对象。
        candidate_ids: 合法候选 node_id 集合。
        raw_query: 用户原始 query。

    Returns:
        (plan, skip_reason)：成功时 skip_reason 为空；失败时 plan 为 None。
    """
    decision = str(parsed.get("decision") or "").strip().lower()
    if decision != "rewrite":
        skip_reason = str(parsed.get("skip_reason") or "llm_decision_skip").strip()
        return None, skip_reason or "llm_decision_skip"

    rewrite_query = str(parsed.get("rewrite_query") or "").strip()
    if not rewrite_query:
        return None, "empty_rewrite_query"

    resolved_refs = parsed.get("resolved_refs")
    if not isinstance(resolved_refs, list) or not resolved_refs:
        return None, "empty_resolved_refs"

    mention_targets: dict[str, str] = {}
    target_nodes: list[str] = []
    normalized_refs: list[dict[str, Any]] = []

    for ref in resolved_refs:
        if not isinstance(ref, dict):
            return None, "invalid_resolved_ref_item"
        mention = str(ref.get("mention") or "").strip()
        target_node = str(ref.get("target_node") or "").strip()
        reason = str(ref.get("reason") or "").strip()
        if not mention or not target_node or not reason:
            return None, "incomplete_resolved_ref"
        resolved_id, resolve_skip = _resolve_target_node(target_node, candidate_ids)
        if resolved_id is None:
            return None, resolve_skip
        target_node = resolved_id
        if mention in mention_targets and mention_targets[mention] != target_node:
            return None, f"ambiguous_mention:{mention}"
        mention_targets[mention] = target_node
        target_nodes.append(target_node)
        normalized_refs.append(
            {
                "mention": mention,
                "target_node": target_node,
                "reason": reason,
            }
        )

    if rewrite_query == raw_query:
        return None, "rewrite_same_as_raw"

    return RewritePlan(
        rewrite_query=rewrite_query,
        resolved_refs=normalized_refs,
        target_nodes=target_nodes,
    ), ""


def _sync_query_node(context: Context, rewrite_query: str) -> bool:
    """将改写后的 query 同步到当前轮 QueryNode。

    Args:
        context: 当前会话 Context。
        rewrite_query: 消解后的 query。

    Returns:
        同步成功返回 True；失败返回 False。
    """
    initial_pt = context.initial_pt
    if not initial_pt:
        return False
    try:
        context.modify_node(graph_node_label=initial_pt, changes={"query": rewrite_query})
    except Exception as exc:
        logger.warning(f"[context_reference_rewriter] modify_node failed: {exc}")
        return False
    return True


def _log_outcome(
    *,
    decision: str,
    raw_user_query: str,
    rewrite_query: str = "",
    candidate_count: int = 0,
    target_nodes: list[str] | None = None,
    reason: str = "",
    skip_reason: str = "",
) -> None:
    """记录指代消解观测日志（debug 级别）。

    Args:
        decision: ``rewrite`` 或 ``skip``。
        raw_user_query: 用户原始输入。
        rewrite_query: 改写后 query（skip 时可为空）。
        candidate_count: 候选数量。
        target_nodes: 目标节点 ID 列表。
        reason: 改写理由摘要。
        skip_reason: 跳过原因。
    """
    logger.debug(
        "[context_reference_rewriter] decision={} raw_user_query={} rewrite_query={} "
        "candidate_count={} target_nodes={} reason={} skip_reason={}",
        decision,
        raw_user_query,
        rewrite_query,
        candidate_count,
        target_nodes or [],
        reason,
        skip_reason,
    )


def context_reference_rewriter(state: FlexState, runtime: Runtime) -> FlexState:
    """Agent pre-hook：消解用户 query 中的上下文指代并写回 ``user_query``。

    subagent（``sub_id != 0``）、无 Context、Stage A 判定无需改写、无候选、
    LLM 失败或校验不通过时均为 no-op。Subagent 路径不调 LLM、不改写。

    Args:
        state: Flex 工作流状态。
        runtime: Flex 运行时。

    Returns:
        可能更新 ``raw_user_query`` / ``user_query`` 后的 state。
    """
    if is_subagent(state):
        return state

    raw_query = _ensure_raw_user_query(state)
    if not raw_query:
        return state

    try:
        return _apply_context_reference_rewrite(state, runtime, raw_query)
    except Exception as exc:
        _log_outcome(
            decision="skip",
            raw_user_query=raw_query,
            skip_reason=f"hook_failed:{type(exc).__name__}",
        )
        return state


def _apply_context_reference_rewrite(
    state: FlexState,
    runtime: Runtime,
    raw_query: str,
) -> FlexState:
    """执行指代消解主逻辑；异常由 ``context_reference_rewriter`` 兜底捕获。

    Args:
        state: Flex 工作流状态。
        runtime: Flex 运行时。
        raw_query: 用户原始 query（已写入 ``raw_user_query``）。

    Returns:
        可能更新 ``user_query`` 后的 state。
    """
    context = get_context_for_flex_state(state, runtime, swallow_errors=True)
    if context is None:
        _log_outcome(
            decision="skip",
            raw_user_query=raw_query,
            skip_reason="no_context",
        )
        return state

    analysis, analyze_skip = _analyze_query_intent(runtime, raw_query)
    if analysis is None:
        _log_outcome(
            decision="skip",
            raw_user_query=raw_query,
            skip_reason=analyze_skip,
        )
        return state

    if not analysis.needs_rewrite:
        _log_outcome(
            decision="skip",
            raw_user_query=raw_query,
            skip_reason=analysis.skip_reason or "no_reference",
        )
        return state

    target_types, temporal_hint = _merge_analysis_filters(analysis.mentions)
    candidates = _collect_candidates(
        context,
        DEFAULT_MAX_CANDIDATES,
        target_types=target_types,
        temporal_hint=temporal_hint,
    )
    if not candidates:
        _log_outcome(
            decision="skip",
            raw_user_query=raw_query,
            candidate_count=0,
            skip_reason="no_candidates",
        )
        return state

    candidate_ids = {str(c["node_id"]) for c in candidates}

    try:
        prompt = _build_llm_prompt(raw_query, candidates, temporal_hint=temporal_hint)
        llm_text = _invoke_llm(runtime, prompt)
    except Exception as exc:
        _log_outcome(
            decision="skip",
            raw_user_query=raw_query,
            candidate_count=len(candidates),
            skip_reason=f"llm_failed:{type(exc).__name__}",
        )
        return state

    parsed = _parse_llm_json(llm_text)

    if parsed is None:
        _log_outcome(
            decision="skip",
            raw_user_query=raw_query,
            candidate_count=len(candidates),
            skip_reason="invalid_json",
        )
        return state

    logger.debug(f"[context_reference_rewriter] llm output: {parsed}")
    plan, skip_reason = _validate_rewrite_plan(parsed, candidate_ids, raw_query)
    if plan is None:
        _log_outcome(
            decision="skip",
            raw_user_query=raw_query,
            candidate_count=len(candidates),
            skip_reason=skip_reason,
        )
        return state

    original_user_query = str(state.get("user_query") or raw_query)
    state["user_query"] = plan.rewrite_query
    if not _sync_query_node(context, plan.rewrite_query):
        state["user_query"] = original_user_query
        _log_outcome(
            decision="skip",
            raw_user_query=raw_query,
            rewrite_query=plan.rewrite_query,
            candidate_count=len(candidates),
            target_nodes=plan.target_nodes,
            skip_reason="query_node_sync_failed",
        )
        return state

    reason_summary = "; ".join(str(ref.get("reason") or "") for ref in plan.resolved_refs[:3])

    logger.debug(
        "[context_reference_rewriter] query replaced: raw_user_query={} user_query={}",
        raw_query,
        state["user_query"],
    )
    _log_outcome(
        decision="rewrite",
        raw_user_query=raw_query,
        rewrite_query=plan.rewrite_query,
        candidate_count=len(candidates),
        target_nodes=plan.target_nodes,
        reason=reason_summary,
    )
    return state
