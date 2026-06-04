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
"""Rich-based CLI renderer for FlexAgent streaming output.

Provides a Cursor/ChatGPT-style terminal experience with:
- Spinner animation while waiting for LLM / tool execution
- Markdown rendering for planner reasoning
- Structured tool-call display with tree layout
- Color-coded success/error feedback
"""

from __future__ import annotations

import contextvars
import json
import re
import time
from typing import Any

from loguru import logger

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.spinner import Spinner
    from rich.table import Table
    from rich.text import Text
    from rich.tree import Tree

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    logger.info("Rich library not found. Install rich with `uv sync --extra cli` to enable enhanced CLI output.")
    Console = None  # type: ignore[assignment]
    Group = None  # type: ignore[assignment]
    Live = None  # type: ignore[assignment]
    Markdown = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Rule = None  # type: ignore[assignment]
    Spinner = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
    Text = None  # type: ignore[assignment]
    Tree = None  # type: ignore[assignment]

from dataagent.utils.constants import (
    DEFAULT_INITIAL_THINKING_MIN_DISPLAY_SECONDS,
    DEFAULT_LIVE_REFRESH_PER_SECOND,
    DEFAULT_MAX_SUBAGENT_HINT_LINES,
    DEFAULT_PLANNER_REFRESH_INTERVAL_SECONDS,
    DEFAULT_RICH_ERROR_TRUNCATION,
    DEFAULT_RICH_RESULT_TRUNCATION,
    DEFAULT_RICH_SCALAR_MAX_LENGTH,
)

_TOOL_CALL_PATTERN = re.compile(r"^\*\*正在调用以下工具:\*\*", re.MULTILINE)
_TOOL_ITEM_PATTERN = re.compile(r"^-\s+\*\*(.+?)\*\*$")
_TOOL_ARGS_PATTERN = re.compile(r"^-\s+args:\s*$")
_COMPLETION_PATTERN = re.compile(r"^\*\*(.+?)\s+执行完成\*\*", re.MULTILINE)
_ERROR_PATTERN = re.compile(r"^\*\*❌\s*(.+?)\*\*", re.MULTILINE)
_RESULT_PATTERN = re.compile(r"^\*\*✅\s*(.+?)\*\*", re.MULTILINE)
_NODE_HEADER_PATTERN = re.compile(r"^\*\*(.+?):\*\*\s*$", re.MULTILINE)
_ANSWER_TAG_PATTERN = re.compile(r"<answer>\s*(.*?)\s*</answer>", re.IGNORECASE | re.DOTALL)


def prepare_model_content_for_markdown(text: str) -> str:
    """将 ``<answer>`` 转为 Markdown 代码块，避免 Rich 将其当作 HTML 吞掉正文。"""
    if not text or "<answer>" not in text.lower():
        return text

    def _repl(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        return f"\n\n**Answer:**\n\n```sql\n{inner}\n```\n"

    return _ANSWER_TAG_PATTERN.sub(_repl, text)


_ACTIVE_RENDERER: contextvars.ContextVar[StreamRenderer | None] = contextvars.ContextVar(
    "dataagent_active_stream_renderer",
    default=None,
)


def set_active_renderer(renderer: StreamRenderer | None) -> contextvars.Token[StreamRenderer | None]:
    """Register the renderer for the current execution context."""
    return _ACTIVE_RENDERER.set(renderer)


def reset_active_renderer(token: contextvars.Token[StreamRenderer | None]) -> None:
    """Restore the previous renderer binding for the current execution context."""
    _ACTIVE_RENDERER.reset(token)


def suspend_active_renderer() -> None:
    """Synchronously pause any active Live spinner before interactive input."""
    renderer = _ACTIVE_RENDERER.get()
    if renderer is not None:
        renderer.suspend()


def resume_active_renderer() -> None:
    """Resume the Live spinner after interactive input completes."""
    renderer = _ACTIVE_RENDERER.get()
    if renderer is not None:
        renderer.resume()


def render_active_human_feedback_prompt(reason: str, pending_action: str = "") -> bool:
    """Render a HITL prompt using the active renderer if available."""
    renderer = _ACTIVE_RENDERER.get()
    if renderer is None:
        return False
    renderer.render_human_feedback_prompt(reason=reason, pending_action=pending_action)
    return True


class StreamRenderer:
    """Stateful renderer that consumes stream events and produces rich terminal output."""

    INITIAL_THINKING_MIN_DISPLAY_SECONDS = DEFAULT_INITIAL_THINKING_MIN_DISPLAY_SECONDS
    PLANNER_REFRESH_INTERVAL_SECONDS = DEFAULT_PLANNER_REFRESH_INTERVAL_SECONDS
    SPINNER_THINKING = "[bold bright_cyan]🤖 {node} 正在思考...[/bold bright_cyan]"
    SPINNER_EXECUTING = "[bold yellow]🔧 正在执行工具...[/bold yellow]"

    # 子 Agent 进度历史最多保留的行数
    MAX_SUBAGENT_HINT_LINES = DEFAULT_MAX_SUBAGENT_HINT_LINES

    def __init__(self, console: Console | None = None):
        self._console = console or Console()
        self._status: Live | None = None
        self._phase: str = "idle"  # idle | thinking | planner_streaming | executing
        self._current_node: str = ""
        self._has_rendered_planner = False
        self._planner_state: dict[str, Any] = {
            "active": False,
            "node_name": "",
            "reasoning": "",
            "content": "",
            "status": "streaming",
        }
        self._tool_states: dict[str, dict[str, Any]] = {}
        self._tool_order: list[str] = []
        self._resume_thinking_after_execution_msg = False
        # When True, we intentionally stop Live updates (e.g. during prompt_toolkit/pdb input)
        self._suspended: bool = False
        self._phase_before_suspend: str = "idle"
        self._planner_last_refresh_at = 0.0
        self._planner_refresh_pending = False
        self._thinking_started_at = 0.0
        self._initial_thinking_guard_active = False

    @staticmethod
    def _format_scalar(value: Any, max_length: int = DEFAULT_RICH_SCALAR_MAX_LENGTH) -> str:
        """Format scalar values for compact tree display."""
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except TypeError:
                text = str(value)
        if len(text) <= max_length:
            return text
        return f"{text[: max_length - 3]}..."

    @staticmethod
    def _parse_tool_entries(content: str) -> list[dict[str, str]]:
        """Parse planner tool-call markdown into tool name + args entries."""
        entries: list[dict[str, str]] = []
        current_entry: dict[str, str] | None = None
        collecting_args = False
        arg_lines: list[str] = []
        for raw_line in content.splitlines():
            stripped_line = raw_line.strip()
            if collecting_args:
                if raw_line.startswith("    "):
                    arg_lines.append(raw_line[4:])
                    continue
                if current_entry is not None:
                    current_entry["args"] = "\n".join(arg_lines).rstrip()
                collecting_args = False
                arg_lines = []
            if not stripped_line:
                continue
            tool_match = _TOOL_ITEM_PATTERN.match(stripped_line)
            if tool_match:
                current_entry = {"name": tool_match.group(1).strip()}
                entries.append(current_entry)
                continue
            args_match = _TOOL_ARGS_PATTERN.match(stripped_line)
            if args_match and current_entry is not None:
                collecting_args = True
                arg_lines = []
        if collecting_args and current_entry is not None:
            current_entry["args"] = "\n".join(arg_lines).rstrip()
        return entries

    @staticmethod
    def _normalize_tool_status(status: str) -> str:
        if status == "start":
            return "running"
        if status in {"running", "success", "error", "pending"}:
            return status
        return "pending"

    @staticmethod
    def _build_status_panel(title: str, border_style: str, message: str, spinner_style: str) -> Panel:
        """Build a panel that contains the live spinner message."""
        grid = Table.grid(padding=(0, 1))
        grid.add_row(Spinner("dots", style=spinner_style), Text.from_markup(message))
        return Panel(
            grid,
            title=title,
            border_style=border_style,
            padding=(1, 2),
        )

    # -- public API ----------------------------------------------------------

    def start(self, initial_node: str = "planner") -> None:
        """Begin rendering session — show initial spinner."""
        self._current_node = initial_node
        self._initial_thinking_guard_active = True
        self._enter_thinking(initial_node)

    def stop(self) -> None:
        """End rendering session — stop any active spinner."""
        self._stop_status()
        self._phase = "idle"

    def suspend(self) -> None:
        """Pause Live updates immediately for terminal-interactive sections."""
        if not self._suspended:
            self._phase_before_suspend = self._phase
        self._suspended = True
        self._stop_status()

    def resume(self) -> None:
        """Resume Live updates after terminal-interactive sections."""
        if not self._suspended:
            return
        self._suspended = False
        if self._phase_before_suspend == "executing":
            self._enter_executing()
        elif self._phase_before_suspend == "planner_streaming":
            self._enter_planner_streaming(self._current_node)
        elif self._phase_before_suspend == "thinking":
            self._enter_thinking(self._current_node)
        else:
            self._stop_status()

    def render_human_feedback_prompt(self, *, reason: str, pending_action: str = "") -> None:
        """Render human-feedback guidance with the same visual style as planner panels."""
        self._stop_status()
        self._render_planner_separator()

        body_lines = [
            "## 需要您的反馈",
            "",
            f"**原因：** {reason}",
        ]
        if pending_action:
            body_lines.extend(["", f"**待确认操作：** {pending_action}"])

        panel = Panel(
            Markdown("\n".join(body_lines)),
            title="[bold cyan]🤖 DataAgent[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
        self._console.print(panel)
        self._has_rendered_planner = True

    def handle_event(self, data: dict[str, Any]) -> None:
        """Dispatch a single stream event dict to the appropriate renderer."""
        msg_type = data.get("type", "")
        if msg_type == "planner_stream":
            self._handle_planner_stream(data)
        elif msg_type == "planner_tool_calls":
            self._handle_planner_tool_calls(data)
        elif msg_type == "planner_error":
            self._handle_planner_error(data)
        elif msg_type == "output_msg":
            self._handle_output_msg(data)
        elif msg_type == "execution_msg":
            self._handle_execution_msg(data)
        elif msg_type == "tool_status":
            self._handle_tool_status(data)
        elif msg_type == "break":
            pass

    def update_subagent_hint(self, tool_call_id: str, hint_text: str) -> None:
        """更新指定工具调用的 Subagent 实时进度提示（供 Runtime 回调注入）。"""
        self._append_subagent_hint(tool_call_id, hint_text)

    def _handle_output_msg(self, data: dict[str, Any]) -> None:
        node_name = data.get("node_name", "")
        content: str = data.get("content", "")
        reasoning_content: str = str(data.get("reasoning_content", "") or "")

        if _TOOL_CALL_PATTERN.search(content):
            if reasoning_content.strip():
                self._render_reasoning_panel_if_any(reasoning_content)
            self._render_tool_calls(data)
            self._enter_executing()
            return

        if _COMPLETION_PATTERN.search(content):
            self._render_tool_completion(content)
            if self._phase == "executing":
                self._finish_executing_if_done(wait_for_execution_msg=True)
            else:
                self._enter_thinking(self._current_node)
            return

        if _ERROR_PATTERN.search(content):
            self._render_error(content)
            if self._phase == "executing":
                self._finish_executing_if_done(wait_for_execution_msg=False)
            return

        self._render_planner_content(node_name, content, reasoning_content=reasoning_content)

    def _handle_execution_msg(self, data: dict[str, Any]) -> None:
        content: str = data.get("content", "")
        if _RESULT_PATTERN.search(content):
            self._render_tool_result(content)
        else:
            self._render_generic(content)
        if self._resume_thinking_after_execution_msg:
            self._resume_thinking_after_execution_msg = False
            self._enter_thinking(self._current_node)

    def _handle_planner_stream(self, data: dict[str, Any]) -> None:
        phase = str(data.get("phase", ""))
        node_name = str(data.get("node_name", "") or self._current_node or "planner")
        content = str(data.get("content", "") or "")

        if phase == "start":
            self._enter_planner_streaming(node_name)
            return
        if phase == "reasoning":
            self._append_planner_stream(reasoning_delta=content, node_name=node_name)
            return
        if phase == "content":
            self._append_planner_stream(content_delta=content, node_name=node_name)
            return
        if phase == "end":
            self._finish_planner_streaming(force_refresh=True)

    def _handle_planner_tool_calls(self, data: dict[str, Any]) -> None:
        self._finish_planner_streaming(status="handoff", force_refresh=True)
        self._render_tool_calls(data)
        self._enter_executing()

    def _handle_planner_error(self, data: dict[str, Any]) -> None:
        self._finish_planner_streaming(status="completed", force_refresh=True)
        content = str(data.get("content", "") or "")
        if content:
            self._render_error(content)

    def _render_reasoning_panel_if_any(self, reasoning_content: str) -> None:
        """Print the magenta reasoning panel when non-empty."""
        text = reasoning_content.strip()
        if not text:
            return
        self._stop_status()
        reasoning_panel = Panel(
            Markdown(text),
            title="[bold magenta]🧠 思考过程[/bold magenta]",
            border_style="magenta",
            padding=(1, 2),
        )
        self._console.print(reasoning_panel)
        self._console.print()

    def _render_planner_content(self, node_name: str, content: str, *, reasoning_content: str = "") -> None:
        """Render planner reasoning content as Markdown inside a styled panel."""
        self._stop_status()
        self._clear_planner_state()
        if self._phase == "executing":
            self._clear_tool_state()
        self._phase = "idle"
        if node_name:
            self._current_node = node_name

        clean = content.strip()
        header_match = _NODE_HEADER_PATTERN.match(clean)
        body = clean[header_match.end() :].strip() if header_match else clean

        # 流式收尾事件可能只有 reasoning_content、正文已在先前 chunk 中输出完毕
        if not body and not str(reasoning_content or "").strip():
            return

        self._render_planner_separator()
        self._render_reasoning_panel_if_any(reasoning_content)
        if not body:
            # reasoning-only event: mark as rendered so the next content panel gets a separator
            if reasoning_content.strip():
                self._has_rendered_planner = True
            return
        md = Markdown(prepare_model_content_for_markdown(body))
        panel = Panel(
            md,
            title="[bold cyan]🤖 DataAgent[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
        self._console.print(panel)
        self._has_rendered_planner = True

    def _render_tool_calls(self, data: dict[str, Any]) -> None:
        """Render tool call list as a rich Tree."""
        self._stop_status()

        tool_calls = data.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            self._prepare_tool_states(tool_calls)
            tree = Tree("[bold yellow]▶ 调用工具[/bold yellow]")
            for tool_call in tool_calls:
                tool_name = str(tool_call.get("name", "unknown"))
                branch = tree.add(f"[bold]{tool_name}[/bold]")
                args_branch = branch.add("[dim]args:[/dim]")
                self._add_args_tree(args_branch, tool_call.get("args", {}))
            self._console.print()
            self._console.print(tree)
            self._console.print()
            return

        content = str(data.get("content", ""))
        tool_entries = self._parse_tool_entries(content)
        if not tool_entries:
            self._render_generic(content)
            return

        tree = Tree("[bold yellow]▶ 调用工具[/bold yellow]")
        for entry in tool_entries:
            branch = tree.add(f"[bold]{entry['name']}[/bold]")
            args_text = entry.get("args")
            if args_text:
                args_branch = branch.add("[dim]args:[/dim]")
                for line in args_text.splitlines():
                    args_branch.add(Text(line or " ", style="default"))

        self._console.print()
        self._console.print(tree)
        self._console.print()

    def _render_tool_completion(self, content: str) -> None:
        """Render tool completion notification."""
        match = _COMPLETION_PATTERN.search(content)
        if match:
            tool_name = match.group(1).strip()
            self._console.print(Text.assemble(("  ✅ ", "bold green"), (f"{tool_name} ", "bold"), ("完成", "green")))
        else:
            self._render_generic(content)

    def _render_tool_result(self, content: str) -> None:
        """Render detailed tool execution result in a panel."""
        clean = content.strip()
        result_match = _RESULT_PATTERN.search(clean)
        if result_match:
            title = result_match.group(1).strip()
            body = clean[result_match.end() :].strip()
        else:
            title = "工具结果"
            body = clean

        if len(body) > DEFAULT_RICH_RESULT_TRUNCATION:
            body = body[:DEFAULT_RICH_RESULT_TRUNCATION] + "\n\n... (输出已截断)"

        if body:
            panel = Panel(
                Markdown(body),
                title=f"[bold green]✅ {title}[/bold green]",
                border_style="green",
                padding=(0, 2),
            )
            self._console.print(panel)

    def _render_error(self, content: str) -> None:
        """Render error message in a red panel."""
        if self._phase != "executing":
            self._stop_status()
        clean = content.strip()
        panel = Panel(
            Markdown(clean),
            title="[bold red]❌ 错误[/bold red]",
            border_style="red",
            padding=(0, 2),
        )
        self._console.print(panel)

    def _render_generic(self, content: str) -> None:
        """Fallback: render as Markdown."""
        if self._phase != "executing":
            self._stop_status()
        clean = content.strip()
        if clean:
            self._console.print(Markdown(clean))

    def _render_planner_separator(self) -> None:
        """Render extra breathing room before planner output."""
        self._console.print()
        if self._has_rendered_planner:
            self._console.print(Rule(style="bright_black"))
            self._console.print()

    def _add_args_tree(self, branch: Tree, value: Any, key: str | None = None) -> None:
        """Render nested args structure as a tree without JSON braces."""
        if isinstance(value, dict):
            target = branch.add(f"[dim]{key}[/dim]") if key is not None else branch
            for child_key, child_value in value.items():
                self._add_args_tree(target, child_value, str(child_key))
            return

        if isinstance(value, list):
            target = branch.add(f"[dim]{key}[/dim]") if key is not None else branch
            for idx, item in enumerate(value):
                self._add_args_tree(target, item, f"[{idx}]")
            return

        formatted_value = self._format_scalar(value)
        if key is None:
            branch.add(Text(formatted_value, style="default"))
        else:
            branch.add(Text.assemble((f"{key}: ", "dim"), (formatted_value, "default")))

    def _enter_thinking(self, node_name: str) -> None:
        self._stop_status()
        self._clear_planner_state()
        self._current_node = node_name or self._current_node
        self._phase = "thinking"
        if self._suspended:
            return
        self._thinking_started_at = time.monotonic()
        self._status = Live(
            self._build_status_panel(
                title=f"[bold cyan]🤖 {self._current_node}[/bold cyan]",
                border_style="cyan",
                message=self.SPINNER_THINKING.format(node=self._current_node),
                spinner_style="bright_cyan",
            ),
            console=self._console,
            transient=True,
            refresh_per_second=DEFAULT_LIVE_REFRESH_PER_SECOND,
        )
        status = self._status
        if status is not None:
            status.start()

    def _enter_planner_streaming(self, node_name: str) -> None:
        if self._phase != "planner_streaming" or self._status is None:
            if self._phase == "thinking" and self._initial_thinking_guard_active:
                elapsed = time.monotonic() - self._thinking_started_at
                remaining = self.INITIAL_THINKING_MIN_DISPLAY_SECONDS - elapsed
                if remaining > 0:
                    time.sleep(remaining)
                self._initial_thinking_guard_active = False
            if self._phase != "planner_streaming":
                self._stop_status()
                self._render_planner_separator()
                self._planner_state = {
                    "active": True,
                    "node_name": node_name or self._current_node or "planner",
                    "reasoning": "",
                    "content": "",
                    "status": "streaming",
                }
                self._phase = "planner_streaming"
                self._planner_last_refresh_at = 0.0
                self._planner_refresh_pending = False
            self._current_node = node_name or self._current_node
            self._planner_state["node_name"] = self._current_node
            if self._suspended:
                return
            self._status = Live(
                self._build_planner_stream_panel(),
                console=self._console,
                transient=False,
                auto_refresh=False,
                vertical_overflow="ellipsis",
                refresh_per_second=DEFAULT_LIVE_REFRESH_PER_SECOND,
            )
            status = self._status
            if status is not None:
                status.start()
            self._has_rendered_planner = True
            return
        if node_name:
            self._planner_state["node_name"] = node_name
            self._current_node = node_name
        self._refresh_planner_stream()

    def _append_planner_stream(
        self,
        *,
        reasoning_delta: str = "",
        content_delta: str = "",
        node_name: str = "",
    ) -> None:
        if self._phase != "planner_streaming" or self._status is None:
            self._enter_planner_streaming(node_name or self._current_node)
        if node_name:
            self._planner_state["node_name"] = node_name
            self._current_node = node_name
        if reasoning_delta:
            self._planner_state["reasoning"] = str(self._planner_state.get("reasoning", "")) + reasoning_delta
        if content_delta:
            self._planner_state["content"] = str(self._planner_state.get("content", "")) + content_delta
        self._planner_state["status"] = "streaming"
        self._refresh_planner_stream()

    def _build_planner_stream_panel(self) -> Panel:
        node_name = str(self._planner_state.get("node_name") or self._current_node or "planner")
        reasoning = str(self._planner_state.get("reasoning", "") or "")
        content = str(self._planner_state.get("content", "") or "")
        status = str(self._planner_state.get("status", "streaming") or "streaming")

        status_grid = Table.grid(padding=(0, 1))
        if status == "streaming":
            status_grid.add_row(
                Spinner("dots", style="bright_cyan"), Text("正在思考并生成回复...", style="bright_cyan")
            )
        elif status == "handoff":
            status_grid.add_row(Text.assemble(("✅ ", "bold green"), ("思考完毕，开始调用工具", "green")))
        else:
            status_grid.add_row(Text.assemble(("✅ ", "bold green"), ("思考完毕", "green")))

        renderables: list[Any] = []
        if reasoning.strip():
            renderables.append(Text("思考过程", style="bold magenta"))
            renderables.append(Text(reasoning.rstrip(), style="bright_black"))
        if content.strip():
            if reasoning.strip():
                renderables.append(Rule(style="bright_black"))
            renderables.append(Markdown(prepare_model_content_for_markdown(content)))
        elif not reasoning.strip():
            renderables.append(Text("等待模型输出...", style="bright_black"))
        renderables.append(Rule(style="bright_black"))
        renderables.append(status_grid)

        return Panel(
            Group(*renderables),
            title=f"[bold cyan]🤖 {node_name}[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )

    def _refresh_planner_stream(self, *, force: bool = False) -> None:
        if self._phase != "planner_streaming" or self._status is None:
            return
        now = time.monotonic()
        if not force and (now - self._planner_last_refresh_at) < self.PLANNER_REFRESH_INTERVAL_SECONDS:
            self._planner_refresh_pending = True
            return
        self._status.update(self._build_planner_stream_panel(), refresh=True)
        self._planner_last_refresh_at = now
        self._planner_refresh_pending = False

    def _finish_planner_streaming(self, *, status: str = "completed", force_refresh: bool = False) -> None:
        if self._phase != "planner_streaming":
            return
        self._planner_state["status"] = status
        if self._status is not None:
            self._refresh_planner_stream(force=force_refresh or self._planner_refresh_pending)
        self._stop_status()
        self._clear_planner_state()
        self._phase = "idle"

    def _enter_executing(self) -> None:
        if self._phase == "executing" and self._status is not None:
            self._refresh_executor_status()
            return
        self._stop_status()
        self._clear_planner_state()
        self._phase = "executing"
        if self._suspended:
            return
        self._status = Live(
            self._build_executor_status_panel(),
            console=self._console,
            transient=True,
            refresh_per_second=DEFAULT_LIVE_REFRESH_PER_SECOND,
        )
        status = self._status
        if status is not None:
            status.start()

    def _stop_status(self) -> None:
        if self._status is not None:
            try:
                self._status.stop()
            except (RuntimeError, OSError):
                logger.warning("Failed to stop status", exc_info=True)
            self._status = None

    def _handle_tool_status(self, data: dict[str, Any]) -> None:
        tool_call_id = str(data.get("tool_call_id", ""))
        if not tool_call_id:
            return

        tool_name = str(data.get("tool_name", "unknown"))
        status_str = str(data.get("status", "pending"))
        error_text = str(data.get("error", "")) or None

        self._upsert_tool_state(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_args=data.get("tool_args", {}) or {},
            status=status_str,
            error=error_text,
            summary=str(data.get("summary", "")) or None,
        )
        if self._phase != "executing" or self._status is None:
            self._enter_executing()
        else:
            self._refresh_executor_status()

    def _prepare_tool_states(self, tool_calls: list[dict[str, Any]]) -> None:
        for tool_call in tool_calls:
            tool_call_id = str(tool_call.get("id", ""))
            if not tool_call_id:
                continue
            self._upsert_tool_state(
                tool_call_id=tool_call_id,
                tool_name=str(tool_call.get("name", "unknown")),
                tool_args=tool_call.get("args", {}) or {},
                status="pending",
            )

    def _upsert_tool_state(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        status: str,
        error: str | None = None,
        summary: str | None = None,
        subagent_hints: list[str] | None = None,
    ) -> None:
        normalized_status = self._normalize_tool_status(status)
        existing = self._tool_states.get(tool_call_id, {})
        if tool_call_id not in self._tool_order:
            self._tool_order.append(tool_call_id)
        existing_hints = list(existing.get("subagent_hints", []))
        self._tool_states[tool_call_id] = {
            **existing,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name or existing.get("tool_name", "unknown"),
            "tool_args": tool_args or existing.get("tool_args", {}),
            "status": normalized_status,
            "error": error or existing.get("error"),
            "summary": summary or existing.get("summary"),
            "subagent_hints": subagent_hints if subagent_hints is not None else existing_hints,
        }

    def _append_subagent_hint(self, tool_call_id: str, hint_text: str) -> None:
        """追加一条子 Agent 进度行（带去重和最大行数限制）。"""
        if tool_call_id not in self._tool_states:
            return
        hints: list[str] = list(self._tool_states[tool_call_id].get("subagent_hints", []))
        # 去重：不连续重复
        if hints and hints[-1] == hint_text:
            return
        hints.append(hint_text)
        # 保留最新 MAX_SUBAGENT_HINT_LINES 行
        if len(hints) > self.MAX_SUBAGENT_HINT_LINES:
            hints = hints[-self.MAX_SUBAGENT_HINT_LINES :]
        self._tool_states[tool_call_id]["subagent_hints"] = hints
        if self._phase == "executing":
            self._refresh_executor_status()
        else:
            self._enter_executing()

    def _build_executor_status_panel(self) -> Panel:
        if not self._tool_order:
            return self._build_status_panel(
                title="[bold yellow]🔧 executor[/bold yellow]",
                border_style="yellow",
                message=self.SPINNER_EXECUTING,
                spinner_style="yellow",
            )

        tool_panels = [self._build_tool_panel(self._tool_states[tool_call_id]) for tool_call_id in self._tool_order]
        return Panel(
            Group(*tool_panels),
            title="[bold yellow]🔧 executor[/bold yellow]",
            border_style="yellow",
            padding=(1, 1),
        )

    def _build_tool_panel(self, tool_state: dict[str, Any]) -> Panel:
        status = str(tool_state.get("status", "pending"))
        tool_name = str(tool_state.get("tool_name", "unknown"))
        tool_args = tool_state.get("tool_args", {}) or {}
        error_text = str(tool_state.get("error", "") or "")
        subagent_hints: list[str] = list(tool_state.get("subagent_hints", []))

        status_grid = Table.grid(padding=(0, 1), expand=True)
        if status == "running":
            status_grid.add_row(Spinner("dots", style="yellow"), Text("执行中...", style="yellow"))
            border_style = "yellow"
        elif status == "pending":
            status_grid.add_row(Spinner("dots", style="bright_black"), Text("准备执行...", style="bright_black"))
            border_style = "bright_black"
        elif status == "success":
            status_grid.add_row(Text.assemble(("✅ ", "bold green"), ("已完成", "green")))
            border_style = "green"
        else:
            status_grid.add_row(Text.assemble(("❌ ", "bold red"), ("执行失败", "red")))
            border_style = "red"

        renderables: list[Any] = [status_grid]

        # Subagent 实时进度历史（最多 MAX_SUBAGENT_HINT_LINES 行，缩进显示在工具状态下方）
        if subagent_hints:
            for hint in subagent_hints:
                renderables.append(
                    Text.assemble(
                        ("  ↳ ", "dim cyan"),
                        (hint, "cyan"),
                    )
                )

        if tool_args:
            args_tree = Tree("[dim]args[/dim]")
            self._add_args_tree(args_tree, tool_args)
            renderables.append(args_tree)
        if error_text:
            renderables.append(
                Text(self._format_scalar(error_text, max_length=DEFAULT_RICH_ERROR_TRUNCATION), style="red")
            )

        return Panel(
            Group(*renderables),
            title=f"[bold]{tool_name}[/bold]",
            border_style=border_style,
            padding=(0, 1),
        )

    def _refresh_executor_status(self) -> None:
        if self._phase == "executing" and self._status is not None:
            self._status.update(self._build_executor_status_panel(), refresh=True)

    def _all_executor_tools_terminal(self) -> bool:
        return bool(self._tool_order) and all(
            self._tool_states.get(tool_call_id, {}).get("status") in {"success", "error"}
            for tool_call_id in self._tool_order
        )

    def _finish_executing_if_done(self, *, wait_for_execution_msg: bool) -> None:
        if not self._all_executor_tools_terminal():
            self._refresh_executor_status()
            return
        self._stop_status()
        self._clear_tool_state()
        self._phase = "idle"
        self._resume_thinking_after_execution_msg = wait_for_execution_msg
        if not wait_for_execution_msg:
            self._enter_thinking(self._current_node)

    def _clear_tool_state(self) -> None:
        self._tool_states.clear()
        self._tool_order.clear()

    def _clear_planner_state(self) -> None:
        self._planner_state = {
            "active": False,
            "node_name": "",
            "reasoning": "",
            "content": "",
            "status": "streaming",
        }
        self._planner_last_refresh_at = 0.0
        self._planner_refresh_pending = False
