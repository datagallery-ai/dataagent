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
"""性能采集（单文件，单一契约）。

* **开关**：是否启用仅由环境变量 ``DATAAGENT_PERFORMANCE_ENABLED`` 决定；``1`` 视为开启，其它一律视为关闭。
* **进程隔离**：启用后落盘路径固定为
  ``{workspace}/.performance/Run{run_id}_Sub{sub_id}.{pid}.jsonl``，
  其中 ``workspace`` 由 ``dataagent.utils.runtime_paths.resolve_flex_performance_dir``
  根据运行时 workspace 或 ``user_id``/``session_id`` 决定。不同进程天然写入不同文件，
  避免共享句柄/行错乱。
* **单一路径契约**：调用方不再传入任何路径参数；路径只能通过 ``user_id``/
  ``session_id``/``run_id``/``sub_id``/workspace（+ 进程 PID）派生，没有别名、没有降级。

写入策略：行缓冲 NDJSON，运行中逐行追加事件，``flush()`` 追加最后一行
``kind="_flush"``（含 metadata + summary）。禁用时零 IO。

日志策略：
* 启用成功落地路径一次性打印 ``INFO``（``[perf] enabled, jsonl=...``），
  方便定位产物文件；
* 单条事件走 ``DEBUG``（完整数据已在 jsonl 里，控制台默认不噪音）；
* 用户可感知的失败（落盘 init / write / summary）一律 ``WARNING``，
  不静默回退。
"""

from __future__ import annotations

__all__ = [
    "PERFORMANCE_FLUSH_KIND",
    "performance_enabled_from_env",
    "PerformanceCollector",
    "create_collector",
    "bind_agent_performance",
    "set_current_collector",
    "reset_current_collector",
    "get_current_collector",
    "bind_current_collector",
    "is_noop",
    "build_state_summary",
    "make_perf_state_holder",
    "update_latest_state_from_stream_item",
    "summarize_llm_usage",
    "merge_subagent_llm_usage",
    "measure_tool",
    "attribute_calls",
    "callable_perf_name",
    "run_in_perf_context",
    "submit_in_perf_context",
]

import contextlib
import contextvars
import functools
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from dataagent.utils.env_utils import get_env_bool
from dataagent.utils.log import logger

# Defer the shared usage-module import to break a circular import:
# performance → llm_manager.usage → llm_manager/__init__ → adapters → performance.
# These are resolved on first use (all callers below are runtime, not import-time).
_summarize_usage_impl: Any = None
_cache_hit_rate_impl: Any = None
_TOKEN_FIELDS: tuple[str, ...] = ()


def _resolve_usage_funcs() -> tuple[Any, Any, tuple[str, ...]]:
    """Lazily import the shared usage module on first call to break the import cycle."""
    global _summarize_usage_impl, _cache_hit_rate_impl, _TOKEN_FIELDS
    if _summarize_usage_impl is None:
        from dataagent.core.managers.llm_manager.usage import (
            TOKEN_FIELDS,
            cache_hit_rate,
            summarize_usage,
        )

        _summarize_usage_impl = summarize_usage
        _cache_hit_rate_impl = cache_hit_rate
        _TOKEN_FIELDS = TOKEN_FIELDS
    return _summarize_usage_impl, _cache_hit_rate_impl, _TOKEN_FIELDS


T = TypeVar("T")

PERFORMANCE_FLUSH_KIND: str = "_flush"
_ENV_SWITCH: str = "DATAAGENT_PERFORMANCE_ENABLED"


def _now_iso() -> str:
    """UTC 时间 ``YYYY-MM-DD HH:MM:SS.mmm``（毫秒级）。"""
    dt = datetime.now(UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{dt.microsecond // 1000:03d}"


def summarize_llm_usage(usage: Any) -> dict[str, int]:
    """把 LLM usage 映射规整为 canonical token 计数字典，含 cache/reasoning 子字段。

    薄包装：委托共享 :mod:`usage` 模块的 :func:`summarize_usage`，保证全链路口径一致。
    """
    su_fn, _, _ = _resolve_usage_funcs()
    return su_fn(usage)


def build_state_summary(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """从 agent state 中提取基础轮次、token 与工具调用统计，含 cache/reasoning 字段。"""
    if not isinstance(state, Mapping):
        state = {}
    input_tokens = output_tokens = total_tokens = 0
    cache_read = cache_creation = reasoning = 0
    for msg in state.get("messages") or []:
        usage = getattr(msg, "usage_metadata", None)
        if not isinstance(usage, Mapping):
            continue
        try:
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
            total_tokens += int(usage.get("total_tokens") or 0)
            cache_read += int(usage.get("input_cache_read_tokens") or 0)
            cache_creation += int(usage.get("input_cache_creation_tokens") or 0)
            reasoning += int(usage.get("output_reasoning_tokens") or 0)
        except (TypeError, ValueError):
            continue
    return {
        "agent": {"num_turns": int(state.get("num_turns") or 0)},
        "llms": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "input_cache_read_tokens": cache_read,
            "input_cache_creation_tokens": cache_creation,
            "output_reasoning_tokens": reasoning,
        },
        "tools": {
            "num_valid_tool_calls": int(state.get("num_valid_tool_calls") or 0),
            "num_invalid_tool_calls": int(state.get("num_invalid_tool_calls") or 0),
        },
    }


def make_perf_state_holder(
    initial: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], Callable[[], Any]]:
    """创建 ``latest["state"]`` 容器及 ``flush_state_provider`` 回调。"""
    latest: dict[str, Any] = {"state": dict(initial) if isinstance(initial, Mapping) else {}}
    return latest, (lambda: latest["state"])


def update_latest_state_from_stream_item(
    item: Any,
    latest: dict[str, Any],
    *,
    accept_modes: frozenset[str] = frozenset({"values"}),
    accept_plain_dict: bool = True,
) -> None:
    """从 LangGraph astream chunk 提取完整 state，供 performance flush summary 使用。"""
    if isinstance(item, tuple) and len(item) == 2:
        mode, data = item
        if mode in accept_modes and isinstance(data, dict):
            latest["state"] = data
    elif accept_plain_dict and isinstance(item, dict) and "error" not in item:
        latest["state"] = item


def performance_enabled_from_env() -> bool:
    """是否开启性能采集：仅 ``1``/``true``/``yes``/``on`` 为开启。"""
    return get_env_bool(_ENV_SWITCH, default=False)


def _resolve_jsonl_path(
    *,
    user_id: str,
    session_id: str,
    run_id: str,
    sub_id: str,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """单一路径契约：``{workspace}/.performance/Run{run_id}_Sub{sub_id}.{pid}.jsonl``。

    路径只在启用时由 collector 调用，失败直接抛出由上层捕获。
    """
    from dataagent.utils.runtime_paths import resolve_flex_performance_dir

    base = resolve_flex_performance_dir(
        user_id=user_id,
        session_id=session_id,
        workspace=workspace,
        config=config,
    )
    return base / f"Run{run_id}_Sub{sub_id}.{os.getpid()}.jsonl"


def _pick_cache_control_mode(modes: list[str]) -> str:
    """从观测到的 mode 列表中挑出最有信息量的一个（explicit > implicit > none_or_unknown）。"""
    if not modes:
        return "none_or_unknown"
    for preferred in ("explicit", "implicit", "none_or_unknown"):
        if preferred in modes:
            return preferred
    return "none_or_unknown"


class PerformanceCollector:
    """启用态由构造时决定；禁用态由模块级 ``_NOOP_COLLECTOR`` 单例承载。

    公开调用方不应直接构造启用态的 collector，统一走 :func:`create_collector`。
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | int | None = None,
        sub_id: str | int | None = None,
        backend: str | None = None,
        workspace: str | Path | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        """初始化 collector，并在启用时打开当前进程的 jsonl 文件。"""
        self.enabled = bool(enabled)
        self.user_id: str = str(user_id or "anonymous")
        self.session_id: str = str(session_id or "default_session")
        self.run_id: str = str(run_id) if run_id is not None and str(run_id) else uuid.uuid4().hex
        self.sub_id: str = str(sub_id) if sub_id is not None and str(sub_id) else "0"
        self.backend: str = str(backend or "")
        self.started_at: str = _now_iso()
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._subagent_llm_usages: list[dict[str, Any]] = []
        self._seen_subagent_perf_keys: set[tuple[str, ...]] = set()
        self._jsonl_fh: Any = None
        self._created_perf: float = time.perf_counter()
        self.jsonl_path: Path | None = None

        if not self.enabled:
            return
        try:
            self.jsonl_path = _resolve_jsonl_path(
                user_id=self.user_id,
                session_id=self.session_id,
                run_id=self.run_id,
                sub_id=self.sub_id,
                workspace=workspace,
                config=config,
            )
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            self._jsonl_fh = open(self.jsonl_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
            logger.info(f"[perf] enabled, jsonl={self.jsonl_path}")
        except Exception as e:
            logger.warning(f"[perf] init jsonl failed, performance events will only be in-memory: {e}")
            self._jsonl_fh = None

    def __del__(self) -> None:
        """对象回收时尽力关闭文件句柄。"""
        with contextlib.suppress(Exception):
            self._close_jsonl()

    @property
    def events(self) -> list[dict[str, Any]]:
        """返回当前已记录事件的线程安全快照。"""
        with self._lock:
            return list(self._events)

    @contextmanager
    def measure(self, kind: str, name: str, **base_extra: Any) -> Iterator[dict[str, Any]]:
        """kind ∈ agent | node | hook | llm | tool；yield 可变 dict，事件结束时并入 extra。"""
        if not self.enabled:
            h: dict[str, Any] = dict(base_extra)
            yield h
            return
        stack = _measurement_stack.get()
        parent = stack[-1] if stack else None
        handle: dict[str, Any] = dict(base_extra)
        if kind == "llm" and parent is not None:
            handle["caller_kind"] = parent[0]
            handle["caller_name"] = parent[1]
        started = time.perf_counter()
        started_iso = _now_iso()
        exc: BaseException | None = None
        token = _measurement_stack.set((*stack, (str(kind), str(name))))
        try:
            yield handle
        except BaseException as e:
            exc = e
            raise
        finally:
            with contextlib.suppress(Exception):
                _measurement_stack.reset(token)
            extra = dict(handle)
            handle_ok = extra.pop("success", True)
            handle_err = extra.pop("error_type", None)
            success = exc is None and handle_ok is not False
            err_t = type(exc).__name__ if exc is not None else (str(handle_err) if not success and handle_err else None)
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 4)
            if kind == "llm" and elapsed_ms > 0:
                try:
                    ot = int(extra.get("output_tokens") or 0)
                except (TypeError, ValueError):
                    ot = 0
                if ot > 0:
                    extra["tokens_per_sec"] = round(ot / (elapsed_ms / 1000.0), 2)
            ev: dict[str, Any] = {
                "kind": kind,
                "name": str(name),
                "run_id": self.run_id,
                "sub_id": self.sub_id,
                "pid": os.getpid(),
                "elapsed_ms": elapsed_ms,
                "success": success,
                "started_at": started_iso,
                "ended_at": _now_iso(),
                "extra": extra,
            }
            if err_t:
                ev["error_type"] = err_t
            self._append_event(ev)

    def snapshot_summary(self, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """返回 ``build_summary`` 的只读快照（供子进程 ``summary_sink`` 回传）。"""
        return self.build_summary(state)

    def merge_subagent_llm_usage(self, perf_summary: Any, identity: Mapping[str, Any] | None) -> bool:
        """幂等合并子进程回传的 ``perf_summary`` 到 ``_subagent_llm_usages``。

        校验 schema、token 非负整数、identity 未重复后写入。父进程聚合失败
        SHALL NOT 影响业务返回（调用方应捕获并记录 warning）。

        Args:
            perf_summary: 子进程 ``WorkerResult.perf_summary`` dict（含 ``schema_version`` /
                ``source`` / identity / ``llms`` 顶层聚合）。
            identity: 父进程侧 identity（``parent_session_id`` / ``parent_run_id`` /
                ``tool_call_id`` / ``sub_id`` / ``worker_session_id`` / ``worker_run_id``）。

        Returns:
            ``True`` 表示已聚合；``False`` 表示跳过（重复 / 非法 schema / 负数 token）。
        """
        if not isinstance(perf_summary, Mapping) or not isinstance(identity, Mapping):
            logger.warning("[perf] merge_subagent: perf_summary/identity not a mapping, skipped")
            return False
        llms = perf_summary.get("llms")
        if not isinstance(llms, Mapping):
            logger.warning("[perf] merge_subagent: missing llms aggregate, skipped")
            return False
        _, _, token_fields = _resolve_usage_funcs()
        try:
            schema_version = int(perf_summary.get("schema_version") or 0)
        except (TypeError, ValueError):
            schema_version = 0
        if schema_version < 1:
            logger.warning("[perf] merge_subagent: unsupported schema_version={schema_version}, skipped")
            return False
        tokens: dict[str, int] = {}
        for field in token_fields:
            try:
                value = int(llms.get(field) or 0)
            except (TypeError, ValueError):
                value = -1
            if value < 0:
                logger.warning(f"[perf] merge_subagent: negative {field}={llms.get(field)}, skipped")
                return False
            tokens[field] = value
        try:
            call_count = int(llms.get("call_count") or 0)
        except (TypeError, ValueError):
            call_count = 0
        tool_call_id = str(identity.get("tool_call_id") or "")
        worker_session_id = str(identity.get("worker_session_id") or "")
        worker_run_id = str(identity.get("worker_run_id") or "")
        sub_id = str(identity.get("sub_id") or "")
        parent_session_id = str(identity.get("parent_session_id") or "")
        parent_run_id = str(identity.get("parent_run_id") or "")
        query = str(identity.get("query") or "")
        if tool_call_id:
            dedup_key: tuple[str, ...] = (
                parent_session_id,
                parent_run_id,
                tool_call_id,
                sub_id,
                worker_session_id,
                worker_run_id,
            )
        else:
            import hashlib

            fallback = hashlib.sha256(f"{query}|{worker_run_id}|{worker_session_id}".encode()).hexdigest()
            dedup_key = (
                parent_session_id,
                parent_run_id,
                f"__hash__:{fallback}",
                sub_id,
                worker_session_id,
                worker_run_id,
            )
            logger.debug(
                f"[perf] merge_subagent: tool_call_id missing, used query/run/session hash fallback; "
                f"sub_id={sub_id} worker_session={worker_session_id}"
            )
        with self._lock:
            if dedup_key in self._seen_subagent_perf_keys:
                logger.debug(f"[perf] merge_subagent: duplicate identity {dedup_key}, skipped")
                return False
            self._seen_subagent_perf_keys.add(dedup_key)
            self._subagent_llm_usages.append(
                {
                    "schema_version": schema_version,
                    "source": str(perf_summary.get("source") or "subagent"),
                    "agent_type": str(perf_summary.get("agent_type") or ""),
                    "sub_id": sub_id,
                    "parent_session_id": parent_session_id,
                    "parent_run_id": parent_run_id,
                    "worker_session_id": worker_session_id,
                    "worker_run_id": worker_run_id,
                    "tool_call_id": tool_call_id,
                    "provider": str(perf_summary.get("provider") or ""),
                    "model": str(perf_summary.get("model") or ""),
                    "cache_control_mode": str(perf_summary.get("cache_control_mode") or "none_or_unknown"),
                    "status": str(perf_summary.get("status") or "success"),
                    **tokens,
                    "call_count": call_count,
                }
            )
            return True

    def build_summary(self, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """按 kind 汇总事件，输出 ``main_agent`` / ``subagents`` / ``overall`` 三视角。

        * 每个桶 entry 都带 ``count`` / ``elapsed_ms`` (总和) / ``min_ms`` / ``max_ms`` /
        * ``hook``：按 ``hook_scope:hook_phase:name`` 分桶，保留 agent/node/tool 的生命周期边界；
        * ``llm``：以 ``caller_kind:caller_name:llm_name`` 复合键分桶，
          同时按桶累计 input/output/total tokens；没有 caller（孤儿调用）
          时退化为只用 ``llm_name`` 作键。
        * 顶层 ``input_tokens`` 等兼容字段语义升级为 ``overall``（主 Agent + 子 Agent）。
        * ``llms.main_agent``：父进程自身 ``kind == "llm"`` 事件聚合。
        * ``llms.subagents``：来自 ``_subagent_llm_usages``，``by_agent`` 可按
          ``agent_type:sub_id:worker_session_id`` 下钻。
        * ``llms.overall``：``main_agent + subagents`` 逐字段相加。
        * 所有 ``cache_hit_rate`` 由共享 :func:`cache_hit_rate` 计算，0-1 小数。
        """
        base = build_state_summary(state)
        state_llms = dict(base["llms"])
        summary: dict[str, Any] = {
            "agent": dict(base["agent"]),
            "nodes": {},
            "hooks": {},
            "llms": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "input_cache_read_tokens": 0,
                "input_cache_creation_tokens": 0,
                "output_reasoning_tokens": 0,
                "state_messages": state_llms,
            },
            "tools": dict(base["tools"]),
        }
        with self._lock:
            events = list(self._events)
            subagent_usages = list(self._subagent_llm_usages)

        _, cache_hit_rate_fn, token_fields = _resolve_usage_funcs()
        main_tokens: dict[str, int] = dict.fromkeys(token_fields, 0)
        main_call_count = 0
        main_cache_modes: list[str] = []

        def accumulate(entry: dict[str, Any], ev: dict[str, Any]) -> None:
            """把单条事件累加到对应 summary 桶。"""
            elapsed = float(ev["elapsed_ms"])
            entry["count"] = entry.get("count", 0) + 1
            entry["elapsed_ms"] = round(entry.get("elapsed_ms", 0.0) + elapsed, 4)
            cur_min = entry.get("min_ms")
            entry["min_ms"] = round(elapsed if cur_min is None else min(cur_min, elapsed), 4)
            cur_max = entry.get("max_ms")
            entry["max_ms"] = round(elapsed if cur_max is None else max(cur_max, elapsed), 4)
            if not ev["success"]:
                entry["error_count"] = entry.get("error_count", 0) + 1

        for ev in events:
            kind = ev["kind"]
            if kind == "agent":
                accumulate(summary["agent"], ev)
                continue
            if kind in ("node", "tool"):
                bucket = summary["nodes" if kind == "node" else "tools"]
                entry = bucket.setdefault(ev["name"], {"count": 0, "elapsed_ms": 0.0, "error_count": 0})
                accumulate(entry, ev)
                continue
            if kind == "hook":
                extra = ev.get("extra") or {}
                scope = str(extra.get("hook_scope") or "unknown")
                phase = str(extra.get("hook_phase") or "unknown")
                key = f"{scope}:{phase}:{ev['name']}"
                entry = summary["hooks"].setdefault(
                    key,
                    {
                        "count": 0,
                        "elapsed_ms": 0.0,
                        "error_count": 0,
                        "hook_scope": scope,
                        "hook_phase": phase,
                    },
                )
                accumulate(entry, ev)
                continue
            if kind == "llm":
                extra = ev.get("extra") or {}
                name = str(ev.get("name") or "unknown")
                ck, cn = extra.get("caller_kind"), extra.get("caller_name")
                key = f"{ck}:{cn}:{name}" if ck and cn else name
                entry = summary["llms"].get(key)
                if entry is None:
                    entry = {
                        "count": 0,
                        "elapsed_ms": 0.0,
                        "error_count": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "input_cache_read_tokens": 0,
                        "input_cache_creation_tokens": 0,
                        "output_reasoning_tokens": 0,
                        "llm_name": name,
                    }
                    if ck:
                        entry["caller_kind"] = str(ck)
                    if cn:
                        entry["caller_name"] = str(cn)
                    summary["llms"][key] = entry
                accumulate(entry, ev)
                main_call_count += 1
                ccm = str(extra.get("cache_control_mode") or "")
                if ccm and ccm not in main_cache_modes:
                    main_cache_modes.append(ccm)
                for k in token_fields:
                    try:
                        inc = int(extra.get(k) or 0)
                    except (TypeError, ValueError):
                        inc = 0
                    if inc:
                        main_tokens[k] = main_tokens.get(k, 0) + inc
                        entry[k] = entry.get(k, 0) + inc

        sub_tokens: dict[str, int] = dict.fromkeys(token_fields, 0)
        sub_call_count = 0
        sub_cache_modes: list[str] = []
        by_agent: dict[str, dict[str, Any]] = {}
        for usage in subagent_usages:
            sub_call_count += int(usage.get("call_count") or 0) or 1
            scm = str(usage.get("cache_control_mode") or "")
            if scm and scm not in sub_cache_modes:
                sub_cache_modes.append(scm)
            for field in token_fields:
                sub_tokens[field] += int(usage.get(field) or 0)
            agent_type = str(usage.get("agent_type") or "unknown")
            agent_key = f"{agent_type}:{usage.get('sub_id', '')}:{usage.get('worker_session_id', '')}"
            bucket = by_agent.setdefault(
                agent_key,
                dict.fromkeys(token_fields, 0) | {"call_count": 0, "identity": {}},
            )
            bucket["call_count"] += int(usage.get("call_count") or 0) or 1
            for field in token_fields:
                bucket[field] += int(usage.get(field) or 0)
            bucket["identity"].update(
                {
                    "agent_type": agent_type,
                    "sub_id": str(usage.get("sub_id") or ""),
                    "worker_session_id": str(usage.get("worker_session_id") or ""),
                    "worker_run_id": str(usage.get("worker_run_id") or ""),
                    "provider": str(usage.get("provider") or ""),
                    "model": str(usage.get("model") or ""),
                    "cache_control_mode": str(usage.get("cache_control_mode") or "none_or_unknown"),
                    "status": str(usage.get("status") or "success"),
                }
            )

        overall_tokens: dict[str, int] = {field: main_tokens[field] + sub_tokens[field] for field in token_fields}
        for field in token_fields:
            summary["llms"][field] = overall_tokens[field]
        summary["llms"]["main_agent"] = {
            **main_tokens,
            "call_count": main_call_count,
            "cache_hit_rate": cache_hit_rate_fn(main_tokens),
            "cache_control_mode": _pick_cache_control_mode(main_cache_modes),
        }
        summary["llms"]["subagents"] = {
            **sub_tokens,
            "call_count": sub_call_count,
            "cache_hit_rate": cache_hit_rate_fn(sub_tokens),
            "cache_control_mode": _pick_cache_control_mode(sub_cache_modes),
            "by_agent": by_agent,
        }
        summary["llms"]["overall"] = {
            **overall_tokens,
            "call_count": main_call_count + sub_call_count,
            "cache_hit_rate": cache_hit_rate_fn(overall_tokens),
            "cache_control_mode": _pick_cache_control_mode(main_cache_modes),
        }
        # 断言顶层兼容字段 == overall 对应字段
        for field in token_fields:
            assert summary["llms"][field] == summary["llms"]["overall"][field], (
                f"top-level {field} != overall: {summary['llms'][field]} != {summary['llms']['overall'][field]}"
            )
        return summary

    def flush(self, state: Mapping[str, Any] | None = None) -> Path | None:
        """写入 flush footer、关闭 jsonl，并返回最终产物路径。"""
        if not self.enabled or self._jsonl_fh is None:
            self._close_jsonl()
            return None
        try:
            summary = self.build_summary(state)
        except Exception as e:
            logger.warning(f"[perf] summary failed, footer will carry empty summary: {e}")
            summary = {}
        footer = {
            "kind": PERFORMANCE_FLUSH_KIND,
            "metadata": {
                "user_id": self.user_id,
                "session_id": self.session_id,
                "run_id": self.run_id,
                "sub_id": self.sub_id,
                "pid": os.getpid(),
                "backend": self.backend,
                "started_at": self.started_at,
                "ended_at": _now_iso(),
                "e2e_ms": round((time.perf_counter() - self._created_perf) * 1000.0, 4),
            },
            "summary": summary,
        }
        path = self.jsonl_path
        try:
            with self._lock:
                self._jsonl_fh.write(json.dumps(footer, ensure_ascii=False, default=str) + "\n")
                self._jsonl_fh.flush()
        except Exception as e:
            logger.warning(f"[perf] flush footer failed: {e}")
            path = None
        self._close_jsonl()
        return path

    def _append_event(self, event: dict[str, Any]) -> None:
        """追加单条事件到内存，并在启用 jsonl 时同步落盘。"""
        try:
            line = json.dumps(event, ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning(f"[perf] serialize failed for {event.get('kind')}/{event.get('name')}: {e}")
            line = None
        with self._lock:
            self._events.append(event)
            fh = self._jsonl_fh
            if fh is not None and line is not None:
                try:
                    fh.write(line + "\n")
                except Exception as e:
                    logger.warning(f"[perf] write failed: {e}")
        tail = f" err={event['error_type']}" if event.get("error_type") else ""
        logger.debug(f"[perf] {event['kind']}/{event['name']} {event['elapsed_ms']}ms ok={event['success']}{tail}")

    def _close_jsonl(self) -> None:
        """关闭当前 jsonl 文件句柄；重复调用安全。"""
        fh, self._jsonl_fh = self._jsonl_fh, None
        if fh is None:
            return
        try:
            fh.flush()
            fh.close()
        except Exception as e:
            logger.debug(f"[perf] close jsonl failed: {e}")


_NOOP_COLLECTOR: PerformanceCollector = PerformanceCollector(enabled=False)

_current_collector: contextvars.ContextVar[PerformanceCollector] = contextvars.ContextVar(
    "dataagent_performance_collector", default=_NOOP_COLLECTOR
)
_measurement_stack: contextvars.ContextVar[tuple[tuple[str, str], ...]] = contextvars.ContextVar(
    "dataagent_performance_measurement_stack", default=()
)


def is_noop(collector: PerformanceCollector | None) -> bool:
    """判断 collector 是否为空或处于禁用态。"""
    return collector is None or not collector.enabled


def run_in_perf_context(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """在子线程/线程池中执行 ``fn``，并复制当前 contextvars（含 collector 与 measurement 栈）。"""
    return contextvars.copy_context().run(fn, *args, **kwargs)


def submit_in_perf_context(executor: Any, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> Any:
    """``executor.submit`` 的 perf 安全版本：子线程内可见当前请求的 collector。"""
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)


def callable_perf_name(target: Any) -> str:
    """从任意可调用对象（函数、bound method、partial、callable 实例）推断稳定的展示名。

    用于给 ``measure``/``attribute_calls`` 提供一个不需要业务侧显式传入的名字，
    适用于所有"框架自动包一层 measurement"的场景（BaseNode hook、agent hook、
    其他 callable-driven pipeline）。

    解析顺序：
    1) ``__name__`` —— 普通函数 / lambda（``<lambda>``）
    2) ``name`` 属性 —— 业务对象常见的自描述字段
    3) ``functools.partial.func.__name__`` —— 偏函数
    4) 类名兜底
    """
    for attr in ("__name__", "name"):
        value = getattr(target, attr, None)
        if isinstance(value, str) and value:
            return value
    func = getattr(target, "func", None)
    if func is not None:
        nested = getattr(func, "__name__", None)
        if isinstance(nested, str) and nested:
            return nested
    return type(target).__name__


@contextmanager
def attribute_calls(kind: str, name: str) -> Iterator[None]:
    """Lightweight caller-attribution scope: push (kind, name) onto the measurement stack
    so any nested ``measure("llm", ...)`` records this as its ``caller_kind``/``caller_name``,
    without emitting its own performance event.

    Use this for "funnel" call sites (e.g. ``BaseIR.llm_infer_async``, compression utils,
    perceptor entries) where the surrounding code is just a thin wrapper around an LLM
    call—an extra timing event would only duplicate the LLM event below.

    Heavier wrappers that genuinely add work (hooks, nodes, tools) should keep using
    :meth:`PerformanceCollector.measure`.
    """
    stack = _measurement_stack.get()
    token = _measurement_stack.set((*stack, (str(kind), str(name))))
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            _measurement_stack.reset(token)


def create_collector(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    run_id: str | int | None = None,
    sub_id: str | int | None = None,
    backend: str | None = None,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> PerformanceCollector:
    """开关只看 :func:`performance_enabled_from_env`；路径由进程 PID + 会话信息派生。"""
    if not performance_enabled_from_env():
        return _NOOP_COLLECTOR
    return PerformanceCollector(
        enabled=True,
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        sub_id=sub_id,
        backend=backend,
        workspace=workspace,
        config=config,
    )


@contextmanager
def bind_agent_performance(
    agent: Any,
    *,
    state: Mapping[str, Any] | None = None,
    backend: str | None = None,
    flush_state_provider: Any = None,
    summary_sink: Any = None,
) -> Iterator[PerformanceCollector]:
    """绑定 collector；``user_id``/``session_id``/``run_id``/``sub_id`` 仅从 state 读取。

    ``summary_sink``：可选 callable，在 ``finally`` 中先 snapshot 局部 summary 再回调，
    最后继续 ``flush()`` 写 footer。子进程用它把本次请求的 summary 回传给调用方，
    避免把 summary 写到长期存活的 Agent 实例字段（见设计文档 §7）。
    """
    st = state if isinstance(state, Mapping) else {}
    collector = create_collector(
        user_id=st.get("user_id"),
        session_id=st.get("session_id"),
        run_id=st.get("run_id"),
        sub_id=st.get("sub_id", 0),
        backend=backend,
        workspace=st.get("workspace"),
    )
    token = set_current_collector(collector)
    try:
        with collector.measure("agent", type(agent).__name__):
            yield collector
    finally:
        latest: Any = state
        if callable(flush_state_provider):
            try:
                latest = flush_state_provider() or state
            except Exception as e:
                logger.debug(f"[perf] state provider failed: {e}")
        latest_mapping = latest if isinstance(latest, Mapping) else None
        if callable(summary_sink):
            try:
                snapshot = collector.snapshot_summary(latest_mapping)
                summary_sink(snapshot)
            except Exception as e:
                logger.debug(f"[perf] summary_sink failed: {e}")
        try:
            collector.flush(latest_mapping)
        except Exception as e:
            logger.debug(f"[perf] flush failed: {e}")
        reset_current_collector(token)


def set_current_collector(collector: PerformanceCollector | None) -> contextvars.Token[PerformanceCollector]:
    """把 collector 绑定到当前 context，并返回可恢复的 token。"""
    return _current_collector.set(collector if collector is not None else _NOOP_COLLECTOR)


def reset_current_collector(token: contextvars.Token[PerformanceCollector]) -> None:
    """用 token 恢复当前 context 中的 collector。"""
    with contextlib.suppress(Exception):
        _current_collector.reset(token)


def get_current_collector() -> PerformanceCollector:
    """获取当前 context 的 collector，缺省返回禁用态单例。"""
    return _current_collector.get() or _NOOP_COLLECTOR


def merge_subagent_llm_usage(perf_summary: Any, identity: Mapping[str, Any] | None) -> bool:
    """把子进程 ``perf_summary`` 幂等合并到当前 context 的 collector。

    父进程聚合入口（``tools.py::_merge_subagent_perf_summary`` 的薄包装）。
    聚合失败 SHALL NOT 影响业务返回——调用方应捕获异常并记录 warning。
    """
    return get_current_collector().merge_subagent_llm_usage(perf_summary, identity)


@contextmanager
def bind_current_collector(collector: PerformanceCollector | None) -> Iterator[PerformanceCollector]:
    """在上下文范围内临时绑定当前 collector。"""
    bound = collector if collector is not None else _NOOP_COLLECTOR
    token = set_current_collector(bound)
    try:
        yield bound
    finally:
        reset_current_collector(token)


def measure_tool(fn: Any) -> Any:
    """装饰异步工具调用，将工具执行结果记录为 performance event。"""

    @functools.wraps(fn)
    async def aw(self: Any, tool_call: Any, *args: Any, **kwargs: Any) -> Any:
        """执行被包装的工具函数并补充采集元数据。"""
        with get_current_collector().measure(
            "tool",
            str(tool_call["name"]),
            tool_call_id=str(tool_call["id"]),
        ) as h:
            ex = await fn(self, tool_call, *args, **kwargs)
            meta = getattr(ex, "metadata", None) or {}
            h.update(
                tool_call_id=getattr(ex, "tool_call_id", h.get("tool_call_id")),
                source=meta.get("source") if isinstance(meta, Mapping) else None,
                success=bool(getattr(ex, "success", True)),
            )
            err = getattr(ex, "error_type", None)
            if err:
                h["error_type"] = err
            retry = getattr(ex, "retry_info", None)
            if retry:
                h["retry_info"] = dict(retry) if isinstance(retry, Mapping) else retry
            return ex

    return aw
