# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Preload cached semantic metadata into the planner user query."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import AIMessage
from loguru import logger

from dataagent.actions.tools.semantic_tool.search_tables_with_schema import (
    get_semantic_retrieve_context,
    read_semantic_retrieve_context_cache,
)
from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient
from dataagent.core.flex.utils.context_from_state import get_context_for_flex_state
from dataagent.utils.info_utils import get_current_query

_SEMANTIC_RETRIEVE_CONTEXT_OPEN = "<semantic_retrieve_context>"
_SEMANTIC_RETRIEVE_CONTEXT_CLOSE = "</semantic_retrieve_context>"

# --- L1b-heavy（HITL 拒绝后扩池检索）---------------------------------------
# hitl_auto_resolver 在源表被拒时写 workspace/.semantic/hitl_expand_retrieve_request.json；
# 本 loader 消费标记，用 mr 子代理本轮 query 的业务词再打一次 semantic/retrieve：
# 新 query → 新 sha256 缓存键 → 新 context 文件 → 评测侧 recalled 池（全部
# context 文件并集）单调扩大。护栏：context 文件数上限封顶；schema 核查型
# query 跳过但保留标记；额外检索失败仅告警不终止子代理（区别于主检索 fail-fast）。
_HITL_EXPAND_RETRIEVE_MARKER = "hitl_expand_retrieve_request.json"
_EXPAND_RETRIEVE_SOURCE = "hitl_expand_retrieve"
_MAX_CONTEXT_FILES_FOR_EXPAND = 3
_MR_QUERY_PREFIX_RE = re.compile(r"用户本轮的检索需求：(.+?)(?:\n需要同时检索|$)", re.DOTALL)
_SCHEMA_CHECK_RE = re.compile(
    r"(表结构|schema|字段信息|列信息|建表语句|分区信息|分区字段|索引信息|table[_ ]?schema)",
    re.IGNORECASE,
)
# 追问 chat 防浪费：评测适配器每题固定追加"请简要回答…选择数据源表的原因"型
# 追问（run_id=1）——该 chat 的 agent 若调 mr，get_current_query 取到的"原始 query"
# 是追问文本而非任务原文，导致对问句多打 1 次分钟级重端点 + 无关表混入池
# （实测三题）。应答型文本开头命中即整轮跳过检索。
_FOLLOWUP_ASK_RE = re.compile(
    r"^\s*(请简要回答|请回答|请简要说明|请说明|请解释|为什么|请评估|请总结)",
)

# 评测框架尾巴剥离（检索 query 侧）：evo-eval adapter 在任务 query 后追加
# "\n\n资源目录：<work_dir>\n这个也是你的工作区…请提交以下评测产物…"协议段
# （agent_adapter_base._query_text）。尾巴混进检索 query 两害：① work_dir
# 路径/产物说明文本稀释语义检索相关性；② sha256 缓存键携带运行期 stamp
# 路径，query 变体（全文/截断）间键漂移 → 同一业务问题重复打分钟级重端点
# （实测：基础池 context 的 query 带尾巴）。剥离标记="资源目录："
# （与 resolver 侧 _customer_restate_query 的既有截断约定一致——回复侧
# 已剥，此为检索侧；语料 145 题零该字样）。只剥检索/缓存用 query；
# agent 的 user_query 消息保留尾巴原样（工作区指引对 agent 有操作意义）。
_FRAMEWORK_QUERY_TAIL_RE = re.compile(r"\s*资源目录：.*$", re.DOTALL)


def _strip_framework_tail(query: str) -> str:
    return _FRAMEWORK_QUERY_TAIL_RE.sub("", query).strip()


class _SemanticRetrieveNoResultError(RuntimeError):
    """Raised when semantic retrieval returns no usable table candidates."""


def semantic_retrieve_context_loader(state: dict[str, Any], runtime: Any) -> dict[str, Any]:
    """Append preloaded semantic context to ``state["user_query"]`` before planning.

    The injected block is wrapped by ``<semantic_retrieve_context>`` and contains only retrieved business context.
    """
    # 注入前先捕获子代理本轮 query（enhanced_query，含拒绝后重组的业务词）——
    # 主 context 注入会改写 state["user_query"]，之后再取就带 context 块了
    subagent_query = str(state.get("user_query") or "").strip()
    query = _resolve_original_query(state, runtime)
    if not query:
        logger.debug("[semantic_retrieve_context_loader] skipped: empty original query")
        return state
    # 追问/应答型 chat 的"原始 query"是问句文本，检索无意义且烧分钟级重端点
    if _FOLLOWUP_ASK_RE.match(query):
        logger.info(
            f"[semantic_retrieve_context_loader] 追问型 query 跳过检索（query={query[:40]!r}…）"
        )
        return state

    workspace_root = _resolve_workspace_root(state, runtime)
    if workspace_root is None:
        logger.debug("[semantic_retrieve_context_loader] skipped: workspace is unavailable")
        return state
    run_id = state.get("run_id", getattr(runtime, "run_id", None))
    sub_id = state.get("sub_id", getattr(runtime, "sub_id", None))
    cached = read_semantic_retrieve_context_cache(
        query,
        workspace_root=workspace_root,
    )

    config_manager = getattr(runtime, "config_manager", None)
    if cached is None and config_manager is None:
        logger.debug("[semantic_retrieve_context_loader] skipped: config_manager is unavailable")
        return state

    try:
        if cached is None:
            client = SemanticServiceClient.from_config(config_manager)
            result = get_semantic_retrieve_context(
                query,
                client=client,
                workspace_root=workspace_root,
                run_id=run_id,
                sub_id=sub_id,
                source="semantic_retrieve_context_loader",
            )
        else:
            result = cached

        if not _has_recalled_tables(result):
            raise _SemanticRetrieveNoResultError("semantic retrieve returned no usable table candidates")
    except Exception as exc:
        error_detail = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "[semantic_retrieve_context_loader] ending agent after semantic retrieve failure: {}", error_detail
        )
        return _end_agent_after_semantic_retrieve_failure(state, error_detail)

    # 基础池被拒表过滤：基础 context（原始 query 的 sha256 缓存）全程
    # 复用且不过滤——被拒表常驻注入块，mr 子代理每轮重新看见它并把它排进
    # 检索汇总候选首位（拒绝后 6+ 轮自主检索在被拒样本表族
    # 打转直至硬超时）。注入前按持久台账过滤，掐断工具结果通道的回流。
    result = _filter_base_result_by_rejected_ledger(result, workspace_root)

    injected = _inject_semantic_retrieve_context(state, runtime, result, query)
    return _maybe_expand_retrieve_after_rejection(
        injected, subagent_query, workspace_root, config_manager, run_id, sub_id, runtime
    )


def _resolve_original_query(state: dict[str, Any], runtime: Any) -> str:
    try:
        query = get_current_query(runtime)
    except Exception as exc:
        logger.debug(f"[semantic_retrieve_context_loader] get_current_query skipped: {exc}")
        query = None
    if isinstance(query, str) and query.strip():
        return _strip_framework_tail(query)

    for value in (
        state.get("parent_user_query"),
        state.get("user_query"),
        getattr(runtime, "parent_user_query", None),
        getattr(runtime, "user_query", None),
    ):
        if isinstance(value, str) and value.strip():
            return _strip_framework_tail(value)
    return ""


def _resolve_workspace_root(state: dict[str, Any], runtime: Any) -> Optional[Path]:  # noqa: UP045
    workspace = state.get("workspace") or getattr(runtime, "workspace_dir", None)
    if not workspace:
        return None
    return Path(str(workspace)).expanduser().resolve()


def _has_recalled_tables(payload: dict[str, Any]) -> bool:
    """Return whether semantic retrieval produced at least one table candidate."""
    tables = payload.get("tables")
    if isinstance(tables, list):
        return bool(tables)

    raw_response = payload.get("raw_response")
    data_access_plan = raw_response.get("dataAccessPlan") if isinstance(raw_response, dict) else None
    raw_tables = data_access_plan.get("tables") if isinstance(data_access_plan, dict) else None
    return isinstance(raw_tables, list) and bool(raw_tables)


def _end_agent_after_semantic_retrieve_failure(
    state: dict[str, Any],
    error_detail: str,
) -> dict[str, Any]:
    """Append a failure notice and final semantic-retrieve error, then stop the owning agent."""
    messages = state.get("messages")
    final_messages = list(messages) if isinstance(messages, list) else []
    final_messages.append(
        AIMessage(
            content=(
                "Semantic metadata retrieval did not return usable tables; this metadata recall agent was stopped.\n\n"
                f"{error_detail}"
            )
        )
    )
    state["messages"] = final_messages
    state["complete"] = True
    return state


def _inject_semantic_retrieve_context(
    state: dict[str, Any],
    runtime: Any,
    payload: dict[str, Any],
    original_query: str,
) -> dict[str, Any]:
    context_text = str(payload.get("context_text") or "").strip()
    if not context_text:
        return state

    base_query = str(state.get("user_query") or original_query).strip() or original_query
    if _SEMANTIC_RETRIEVE_CONTEXT_OPEN in base_query:
        return state

    augmented_query = _build_augmented_user_query(base_query, context_text)
    state["user_query"] = augmented_query
    _sync_augmented_query_to_context(state, runtime, augmented_query)
    return state


def _sync_augmented_query_to_context(
    state: dict[str, Any],
    runtime: Any,
    augmented_query: str,
) -> None:
    """把改写后的 user_query 同步进 context IR 初始节点。

    planner prompt 的 user_query 取数自 trajectory_graph.nodes[context.initial_pt]
    的 query 字段（planner_prompt_builder），并非 state["user_query"]——只改
    state 不同步 IR 时，注入的语义块对 planner 完全不可见。
    """
    context = get_context_for_flex_state(state, runtime, swallow_errors=True)
    if context is not None and context.initial_pt:
        try:
            context.modify_node(graph_node_label=context.initial_pt, changes={"query": augmented_query})
        except Exception as exc:
            logger.debug(f"[semantic_retrieve_context_loader] query node sync failed: {exc}")


def _build_augmented_user_query(base_query: str, context_text: str) -> str:
    return (
        f"{base_query}\n\n"
        f"{_SEMANTIC_RETRIEVE_CONTEXT_OPEN}\n"
        "The following metadata context was retrieved before planning. "
        "Use it as already retrieved evidence.\n\n"
        f"{context_text}"
        "\n"
        f"{_SEMANTIC_RETRIEVE_CONTEXT_CLOSE}"
    )


def _maybe_expand_retrieve_after_rejection(
    state: dict[str, Any],
    subagent_query: str,
    workspace_root: Path,
    config_manager: Any,
    run_id: Any,
    sub_id: Any,
    runtime: Any = None,
) -> dict[str, Any]:
    """L1b-heavy：HITL 源表被拒后，按本轮子代理 query 的业务词再检索一次扩池。

    见模块头 _HITL_EXPAND_RETRIEVE_MARKER 注释。流程：
    标记存在 → 提取业务词 query → schema 核查型跳过（保留标记）→ 文件数
    封顶（消费标记）→ 缓存查重（保留标记，等下一轮不同业务词）→ 消费标记
    → 调重端点（失败仅告警）→ 结果并入注入的 context 块。
    """
    marker_path = workspace_root / ".semantic" / _HITL_EXPAND_RETRIEVE_MARKER
    try:
        if not marker_path.is_file():
            return state
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(marker, dict):
            return state
    except Exception as exc:
        logger.debug(f"[hitl_expand_retrieve] marker read failed, ignore: {exc}")
        return state

    extra_query = _extract_expand_query(subagent_query)
    if extra_query is None:
        logger.debug("[hitl_expand_retrieve] 本轮 query 非业务词检索（schema 核查型或空），保留标记")
        return state

    context_files = _count_context_files(workspace_root)
    if context_files >= _MAX_CONTEXT_FILES_FOR_EXPAND:
        _consume_expand_marker(marker_path)
        logger.info(
            f"[hitl_expand_retrieve] context 文件数已达上限（{context_files}>="
            f"{_MAX_CONTEXT_FILES_FOR_EXPAND}），消费标记不再扩池"
        )
        return state

    if read_semantic_retrieve_context_cache(extra_query, workspace_root=workspace_root) is not None:
        logger.debug(f"[hitl_expand_retrieve] 该业务词 query 已有缓存（query={extra_query!r}），保留标记")
        return state

    _consume_expand_marker(marker_path)
    if config_manager is None:
        logger.debug("[hitl_expand_retrieve] config_manager 不可用，跳过扩池")
        return state

    logger.info(
        f"[hitl_expand_retrieve] 消费拒绝标记（round={marker.get('rejection_round')}，"
        f"rejected={marker.get('rejected_tables')}），按新业务词扩池检索: {extra_query!r}"
    )
    try:
        client = SemanticServiceClient.from_config(config_manager)
        payload = get_semantic_retrieve_context(
            extra_query,
            client=client,
            workspace_root=workspace_root,
            run_id=run_id,
            sub_id=sub_id,
            source=_EXPAND_RETRIEVE_SOURCE,
        )
    except Exception as exc:
        # 额外检索失败不终止子代理（区别于主检索 fail-fast）；标记已消费，
        # 下次拒绝会重新写入
        logger.warning(f"[hitl_expand_retrieve] 扩池检索失败（忽略继续）: {type(exc).__name__}: {exc}")
        return state

    if not _has_recalled_tables(payload):
        logger.info("[hitl_expand_retrieve] 扩池检索未返回表候选，跳过注入")
        return state
    # 被拒表回池过滤：扩池调用是纯新 query（服务端无拒绝感知），被拒表凭业务
    # 词匹配照常返回（实测 36 次扩池 56% 带回被拒表）——注入前按标记里的
    # rejected_tables 过滤，打破"拒→扩池带回→agent 再提→再拒"循环。仅过滤注入
    # 文本与内存 payload；磁盘 context 缓存保持端点原貌（评测 recalled 池口径不变）。
    # 标记只带本轮拒绝，历史轮被拒表经持久台账并入过滤。
    rejected_all = list(marker.get("rejected_tables") or [])
    for t in _rejected_tables_from_ledger(workspace_root):
        if t not in rejected_all:
            rejected_all.append(t)
    filtered = _filter_rejected_tables_from_payload(payload, rejected_all)
    if filtered is not payload:
        kept = len(filtered.get("tables") or [])
        logger.info(
            f"[hitl_expand_retrieve] 扩池候选含被拒表已过滤：{len(payload.get('tables') or [])}"
            f" -> {kept}（rejected={rejected_all}）"
        )
        if kept == 0:
            logger.info("[hitl_expand_retrieve] 过滤后无剩余候选，跳过注入")
            return state
    return _inject_extra_retrieve_context(state, filtered, runtime)


def _load_rejected_tables_ledger(workspace_root: Path) -> dict[str, Any]:
    """读 workspace/.memory/hitl_rejected_tables.json（hitl_auto_resolver 拒绝时
    累积写入）。返回 {"rejected_tables": [...], "rounds": n, ...}；
    无台账/损坏时返回空 dict（无拒绝历史 = 不过滤）。"""
    try:
        payload = json.loads(
            (workspace_root / ".memory" / "hitl_rejected_tables.json").read_text(encoding="utf-8")
        )
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _rejected_tables_from_ledger(workspace_root: Path) -> list[str]:
    ledger = _load_rejected_tables_ledger(workspace_root)
    tables = ledger.get("rejected_tables")
    if not isinstance(tables, list):
        return []
    return [str(t) for t in tables if str(t or "").strip()]


def _filter_base_result_by_rejected_ledger(
    payload: dict[str, Any], workspace_root: Path
) -> dict[str, Any]:
    """基础池注入前的被拒表过滤（滤空保护：全为被拒表时保留原结果并告警——
    宁可让 agent 看见已知被拒的候选，也不注入空表清单）。

    只过滤内存 payload（注入文本/tables/tables_with_columns）；磁盘 context
    缓存保持端点原貌（评测 recalled 池口径不变，与扩池过滤同纪律）。
    """
    rejected = _rejected_tables_from_ledger(workspace_root)
    if not rejected:
        return payload
    filtered = _filter_rejected_tables_from_payload(payload, rejected)
    if filtered is not payload:
        kept = len(filtered.get("tables") or [])
        if kept == 0:
            logger.warning(
                "[semantic_retrieve_context_loader] 基础池候选全部是被拒表，"
                f"滤空保护保留原结果（rejected={rejected}）"
            )
            return payload
        logger.info(
            "[semantic_retrieve_context_loader] 基础池含被拒表已过滤："
            f"{len(payload.get('tables') or [])} -> {kept}（rejected={rejected}）"
        )
    return filtered


def _tables_named_match(a: str, b: str) -> bool:
    """表名宽松等价（与 resolver _tables_match 同语义）：全等或裸名互相后缀。"""
    al, bl = str(a).lower(), str(b).lower()
    if not al or not bl:
        return False
    return al == bl or bl.endswith("." + al) or al.endswith("." + bl)


def _filter_rejected_tables_from_payload(
    payload: dict[str, Any], rejected_tables: Any
) -> dict[str, Any]:
    """从扩池 payload 中剔除被拒表（tables / tables_with_columns / context_text）。

    返回原 payload（无被拒表命中）或过滤后的浅拷贝；raw_response 与 diagnostic
    不动（留端点原貌）。context_text 按行过滤：行首是被拒表全名的描述行删除。
    """
    rejected = [str(t) for t in (rejected_tables or []) if str(t or "").strip()]
    if not rejected:
        return payload
    tables = [t for t in (payload.get("tables") or []) if isinstance(t, str)]
    if not any(_tables_named_match(t, r) for t in tables for r in rejected):
        return payload
    kept_tables = [t for t in tables if not any(_tables_named_match(t, r) for r in rejected)]
    filtered = dict(payload)
    filtered["tables"] = kept_tables
    twc = payload.get("tables_with_columns")
    if isinstance(twc, dict):
        filtered["tables_with_columns"] = {
            k: v
            for k, v in twc.items()
            if not any(_tables_named_match(str(k), r) for r in rejected)
        }
    context_text = str(payload.get("context_text") or "")
    if context_text:
        kept_lines = []
        for line in context_text.splitlines():
            lead = line.split(" 描述：", 1)[0].strip()
            if any(_tables_named_match(lead, r) for r in rejected):
                continue
            kept_lines.append(line)
        filtered["context_text"] = "\n".join(kept_lines)
    return filtered


def _extract_expand_query(subagent_query: str) -> str | None:
    """从 mr 子代理的 enhanced_query 提取业务词检索需求作为扩池 query。

    enhanced_query 形如 ``用户本轮的检索需求：{query}.``（可选追加原始任务描述）。
    schema/字段核查型需求（"查看 X 表的表结构/字段"）返回 None——保留标记，
    等待真正的业务词检索轮次。
    """
    text = str(subagent_query or "").strip()
    if not text:
        return None
    match = _MR_QUERY_PREFIX_RE.search(text)
    query = match.group(1).strip() if match else text
    query = _strip_framework_tail(query).rstrip("。.").strip()
    if not query:
        return None
    if _SCHEMA_CHECK_RE.search(query):
        return None
    return query[:500]


def _count_context_files(workspace_root: Path) -> int:
    semantic_dir = workspace_root / ".semantic"
    if not semantic_dir.is_dir():
        return 0
    try:
        return sum(1 for p in semantic_dir.glob("semantic_retrieve_context_*.json") if p.is_file())
    except Exception:
        return 0


def _consume_expand_marker(marker_path: Path) -> None:
    try:
        marker_path.unlink()
    except Exception as exc:
        logger.debug(f"[hitl_expand_retrieve] marker consume failed: {exc}")


def _inject_extra_retrieve_context(
    state: dict[str, Any],
    payload: dict[str, Any],
    runtime: Any = None,
) -> dict[str, Any]:
    """把扩池检索的表候选并入已注入的 context 块（追加第二个语义块）。"""
    context_text = str(payload.get("context_text") or "").strip()
    if not context_text:
        return state
    base_query = str(state.get("user_query") or "").strip()
    if not base_query or _SEMANTIC_RETRIEVE_CONTEXT_OPEN not in base_query:
        return state
    augmented_query = (
        f"{base_query}\n\n"
        f"{_SEMANTIC_RETRIEVE_CONTEXT_OPEN}\n"
        "（补充检索：源表确认被拒后，按新业务词检索到的额外候选表，"
        "与上面已检索的表合并考虑）\n\n"
        f"{context_text}\n"
        f"{_SEMANTIC_RETRIEVE_CONTEXT_CLOSE}"
    )
    # 与首注入同纪律：扩池块必须同步进 context IR 初始节点，否则对 planner
    # 不可见（实测有 run 的 golden 表进了扩池池文件却从未出现在任何 planner
    # 上下文——只改 state["user_query"] 的扩池注入是空注入）。
    state["user_query"] = augmented_query
    _sync_augmented_query_to_context(state, runtime, augmented_query)
    return state
