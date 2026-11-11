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
"""Subagent stderr status extraction — handler-based dispatch.

Each agent output format (ReAct emoji, NL2SQL bare-print, structured
``DA_SUBAGENT_PROGRESS`` protocol, etc.) is encapsulated here. The shared
``extract_subagent_status`` function iterates registered handlers and
returns the first match, deduplicated.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

# ── Utilities ─────────────────────────────────────────────────────────────────

_RE_ANSI_ESCAPE = re.compile(r"\x1b(?:\[@[A-Z]|\[[0-9;?]*[A-Za-z]|\].*?\x07|\x1b)")
# 通用的 loguru 日志级别前缀（跨 agent 类型）
_RE_LOG_LEVEL = re.compile(r"(WARNING|ERROR|CRITICAL|TRACE)")

# 子进程 → 父进程轻量进度协议：DA_SUBAGENT_PROGRESS\t<kind>\t<escaped_payload>
SUBAGENT_PROGRESS_PREFIX = "DA_SUBAGENT_PROGRESS"
_SUBAGENT_PROCESS_NAME = "subagent"


def _strip_ansi_codes(text: str) -> str:
    """Strip ANSI escape codes from text."""
    return _RE_ANSI_ESCAPE.sub("", text)


def _extract_log_level(line: str) -> str | None:
    """Fallback: 提取 loguru 日志级别前缀，适用所有 agent 类型。"""
    m = _RE_LOG_LEVEL.search(line)
    if m:
        level = m.group(1)
        body = line[m.end() :].strip()
        return f"[{level}] {body}"
    return None


def is_subagent_process() -> bool:
    """Return whether the current process was launched as a subagent subprocess."""
    return os.environ.get("DATAAGENT_LOG_PROCESS_NAME") == _SUBAGENT_PROCESS_NAME


def _encode_progress_payload(text: str) -> str:
    """Escape payload so it fits on one stderr line (tab/newline-safe)."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def _decode_progress_payload(text: str) -> str:
    """Inverse of :func:`_encode_progress_payload`."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "r":
                out.append("\r")
            elif nxt == "\\":
                out.append("\\")
            else:
                out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def emit_subagent_progress(kind: str, payload: str = "") -> None:
    """Write one structured progress line to stderr when running as a subagent.

    No-op outside subagent subprocesses. Payload is escaped onto a single line;
    content is not summarized or truncated.
    """
    if not is_subagent_process():
        return
    kind_text = str(kind or "").strip()
    if not kind_text:
        return
    line = f"{SUBAGENT_PROGRESS_PREFIX}\t{kind_text}\t{_encode_progress_payload(payload)}"
    print(line, file=sys.stderr, flush=True)


def _try_parse_progress_protocol(line: str) -> str | None:
    """Parse ``DA_SUBAGENT_PROGRESS`` lines into UI-facing status text.

    Returns ``None`` when *line* is not a protocol line.
    """
    prefix = SUBAGENT_PROGRESS_PREFIX + "\t"
    if not line.startswith(prefix):
        return None
    parts = line.split("\t", 2)
    if len(parts) < 2:
        return None
    kind = parts[1].strip()
    payload = _decode_progress_payload(parts[2]) if len(parts) > 2 else ""
    if kind == "tool_start":
        return f"正在调用 {payload}" if payload else "正在调用工具"
    if kind == "tool_done":
        return f"{payload} 完成" if payload else "完成"
    if kind == "tool_fail":
        return f"{payload} 执行失败" if payload else "执行失败"
    if kind == "planner":
        return payload
    if kind == "reasoning":
        return f"思考：\n{payload}" if payload else "思考："
    return None


# 去重（跨 handler 共享）：同一 tool_call_id 上一次推送的状态文本
_subagent_last_status: dict[str, str] = {}


# ── Handler base class ────────────────────────────────────────────────────────


class _AgentStatusHandler:
    """Base handler for extracting status updates from subagent stderr output.

    Subclasses encapsulate their own regex patterns and per-tool-call state.
    """

    def try_extract(self, line: str, tool_call_id: str) -> str | None:
        """Try to extract a status text from *line*.

        Returns ``None`` when *line* is not recognised by this handler.
        """
        raise NotImplementedError

    def reset(self, tool_call_id: str) -> None:
        """Clean up per-tool-call state when the subprocess finishes."""
        pass

    def reset_all(self) -> None:
        """Clean up all per-tool-call state for this handler."""
        pass


# ── ReAct (FlexAgent) handler ─────────────────────────────────────────────────


class _ReActStatusHandler(_AgentStatusHandler):
    """FlexAgent (ReAct) — Rich-rendered emoji patterns in stderr."""

    _RE_TOOL_START = re.compile(r"(?:▶\s*)?调用工具")
    _RE_TOOL_DONE = re.compile(r"✅\s*([A-Za-z]\w*)\s+完成")
    _RE_TOOL_FAIL = re.compile(r"❌\s*(?:执行失败|(?:([A-Za-z]\w*)\s*)?工具执行失败)")
    _RE_THINKING_DONE = re.compile(r"思考完毕")

    def try_extract(self, line: str, tool_call_id: str) -> str | None:
        if self._RE_TOOL_START.search(line):
            return "正在调用工具"

        m = self._RE_TOOL_DONE.search(line)
        if m:
            return f"{m.group(1).strip()} 完成"

        m = self._RE_TOOL_FAIL.search(line)
        if m:
            tool_name = m.group(1) if m.lastindex and m.group(1) else ""
            return f"{tool_name} 执行失败" if tool_name else "执行失败"

        if self._RE_THINKING_DONE.search(line):
            return "思考完毕"

        return None


# ── NL2SQL (structured agent) handler ─────────────────────────────────────────


class _NL2SQLStatusHandler(_AgentStatusHandler):
    """Structured agent (NL2SQL) — bare ``print()`` with ``=== NodeName ===`` markers."""

    _NODE_NAMES = frozenset(
        {
            "Coordinator",
            "Perceptor",
            "Generator",
            "Validator",
            "Reflector",
            "Executor",
            "Selector",
            "Final Result",
        }
    )
    _RE_MORE_ROWS = re.compile(r"^\.\.\. and (\d+) more rows$")
    # 只保留有意义的行：JSON / SQL 关键字 / Score / 行预览等
    _RE_MEANINGFUL = re.compile(
        r"^(?:\{|\"|SELECT |INSERT |UPDATE |DELETE |CREATE |DROP |WITH |ALTER |"
        r"Score: |schema: |joins: |\[\(|\\d+ row|final_answer|sql|columns|rows)"
    )
    _MAX_CONTENT_LINES = 3

    # 每个 tool_call_id 的 node 上下文和行计数
    _node_context: dict[str, str] = {}
    _node_line_count: dict[str, int] = {}

    def try_extract(self, line: str, tool_call_id: str) -> str | None:
        # === NodeName === 分隔符
        node_header = re.match(r"^===\s+(.+?)\s*===$", line)
        if node_header and node_header.group(1) in self._NODE_NAMES:
            self._node_context[tool_call_id] = node_header.group(1)
            self._node_line_count[tool_call_id] = 0
            return node_header.group(1)

        # ... and N more rows
        more = self._RE_MORE_ROWS.match(line)
        if more:
            node = self._node_context.get(tool_call_id, "")
            rest = f"... and {more.group(1)} more rows"
            return f"{node}: {rest}" if node else rest

        # 已有 node context：仅推送有意义的内容行，且限制行数
        if tool_call_id in self._node_context:
            if not self._RE_MEANINGFUL.match(line):
                return None
            count = self._node_line_count.get(tool_call_id, 0)
            if count >= self._MAX_CONTENT_LINES:
                return None
            self._node_line_count[tool_call_id] = count + 1
            node = self._node_context[tool_call_id]
            return f"{node}: {line}"

        return None

    def reset(self, tool_call_id: str) -> None:
        self._node_context.pop(tool_call_id, None)
        self._node_line_count.pop(tool_call_id, None)

    def reset_all(self) -> None:
        self._node_context.clear()
        self._node_line_count.clear()


# ── Registry ───────────────────────────────────────────────────────────────────

_subagent_handlers: list[_AgentStatusHandler] = [
    _NL2SQLStatusHandler(),  # 更具体的 handler 放前面
    _ReActStatusHandler(),
]


def reset_subagent_status(tool_call_id: str) -> None:
    """清理指定 tool_call_id 的去重缓存和所有 handler 的 per-call 状态。

    由 ``_run_subprocess_async`` 的 ``finally`` 块调用。
    """
    _subagent_last_status.pop(tool_call_id, None)
    for handler in _subagent_handlers:
        handler.reset(tool_call_id)


def extract_subagent_status(line: str, tool_call_id: str, progress_callback: Any) -> None:
    """从子进程 stderr 中提取关键状态行（带去重）。

    优先识别 ``DA_SUBAGENT_PROGRESS`` 协议行（允许超长正文）；其余行仍走
    handler / 日志 fallback，并保留 200 字符上限过滤杂讯。
    """
    if not progress_callback or not tool_call_id:
        return

    line = _strip_ansi_codes(line).strip()
    if not line:
        return

    text = _try_parse_progress_protocol(line)
    if text is None:
        if len(line) > 200:
            return
        for handler in _subagent_handlers:
            text = handler.try_extract(line, tool_call_id)
            if text is not None:
                break
        if text is None:
            text = _extract_log_level(line)

    if not text:
        return

    # 去重：相同状态文本不重复推送
    if _subagent_last_status.get(tool_call_id) == text:
        return
    _subagent_last_status[tool_call_id] = text
    progress_callback(tool_call_id, text)
