# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Rich renderer for DataAgent CLI streaming output."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from loguru import logger

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.spinner import Spinner
    from rich.table import Table
    from rich.text import Text
    from rich.tree import Tree

    RICH_AVAILABLE = True
except ImportError:
    import builtins

    RICH_AVAILABLE = False
    logger.info("Rich library not found. Install rich to enable enhanced CLI output.")

    class Console:  # type: ignore[no-redef]
        @classmethod
        def print(cls, *args: object, **kwargs: object) -> None:
            sep = kwargs.get("sep", " ")
            end = kwargs.get("end", "\n")
            builtins.print(sep.join(str(item) for item in args), end=end)

    Group = None  # type: ignore[assignment]
    Live = None  # type: ignore[assignment]
    Markdown = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Spinner = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
    Text = None  # type: ignore[assignment]
    Tree = None  # type: ignore[assignment]

from dataagent.utils.constants import DEFAULT_LIVE_REFRESH_PER_SECOND, DEFAULT_RICH_RESULT_TRUNCATION

_ANSWER_TAG_PATTERN = re.compile(r"<answer>\s*(.*?)\s*</answer>", re.IGNORECASE | re.DOTALL)
_HEADING_WITHOUT_SPACE_PATTERN = re.compile(r"^(#{1,6})(\S)")


def prepare_model_content_for_markdown(text: str) -> str:
    """Convert model answer tags into Markdown blocks that Rich renders visibly."""
    if not text or "<answer>" not in text.lower():
        return normalize_markdown_for_rich(text)

    def _replace(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        return f"\n\n**Answer:**\n\n```sql\n{inner}\n```\n"

    return normalize_markdown_for_rich(_ANSWER_TAG_PATTERN.sub(_replace, text))


def normalize_markdown_for_rich(text: str) -> str:
    """Normalize only generic Markdown spacing before Rich rendering."""
    if not text:
        return text

    normalized_lines: list[str] = []
    raw_lines = text.splitlines()
    for raw_line in raw_lines:
        line = raw_line.rstrip()
        if line.lstrip().startswith(("#", "|", "```")):
            line = line.lstrip()
        line = _HEADING_WITHOUT_SPACE_PATTERN.sub(r"\1 \2", line)
        stripped = line.strip()
        previous = normalized_lines[-1].strip() if normalized_lines else ""
        is_table_row = stripped.startswith("|") and stripped.endswith("|")
        previous_is_table_row = previous.startswith("|") and previous.endswith("|")
        is_heading = stripped.startswith("#")

        should_insert_blank = (
            (is_table_row and previous and not previous_is_table_row)
            or (is_heading and previous)
            or (previous_is_table_row and not is_table_row)
        )
        if normalized_lines and stripped and should_insert_blank:
            normalized_lines.append("")

        normalized_lines.append(line)

    return "\n".join(normalized_lines)


class StreamRenderer:
    """Stateful Rich renderer for Jiuwen-style DataAgent ``astream`` events."""

    def __init__(self, console: Console | None = None, *, show_reasoning: bool = True) -> None:
        self._console = console or Console()
        self._status: Live | None = None
        self._stream_content = ""
        self._reasoning_content = ""
        self._phase = "idle"
        self._has_rendered_content = False
        self._has_rendered_event = False
        self._has_llm_output = False
        self._show_reasoning = show_reasoning

    @property
    def has_rendered_content(self) -> bool:
        """Return whether assistant content has already been rendered."""
        return self._has_rendered_content

    @staticmethod
    def _extract_content(data: Mapping[str, Any]) -> str:
        content = data.get("content")
        if content is not None:
            return str(content)
        output = data.get("output")
        if output is not None:
            return str(output)
        payload = data.get("payload")
        if isinstance(payload, Mapping):
            payload_content = payload.get("content") or payload.get("output")
            if payload_content is not None:
                return str(payload_content)
        return ""

    @staticmethod
    def _build_waiting_panel() -> Panel:
        grid = Table.grid(padding=(0, 1))
        grid.add_row(Spinner("dots", style="bright_cyan"), Text("Agent 正在思考...", style="bright_cyan"))
        return Panel(
            grid,
            title="[bold cyan]🤖 DataAgent[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )

    @staticmethod
    def _build_stream_panel(content: str) -> Panel:
        body = prepare_model_content_for_markdown(content.rstrip() or "等待模型输出...")
        return Panel(
            Markdown(body),
            title="[bold cyan]🤖 DataAgent[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )

    @staticmethod
    def _build_reasoning_panel(content: str) -> Panel:
        body = content.rstrip() or "正在思考..."
        grid = Table.grid(padding=(0, 1))
        grid.add_row(Spinner("dots", style="magenta"), Text("思考中...", style="magenta"))
        return Panel(
            Group(Text(body, style="bright_black"), grid),
            title="[bold magenta]思考过程[/bold magenta]",
            border_style="magenta",
            padding=(1, 2),
        )

    @staticmethod
    def _build_tool_tree(tool_calls: list[dict[str, Any]]) -> Tree:
        tree = Tree("[bold yellow]▶ 调用工具[/bold yellow]")
        for tool_call in tool_calls:
            tool_name = str(tool_call.get("name", "unknown"))
            branch = tree.add(f"[bold]{tool_name}[/bold]")
            args = tool_call.get("args", {})
            if isinstance(args, Mapping) and args:
                args_branch = branch.add("[dim]args[/dim]")
                StreamRenderer._add_tree_value(args_branch, args)
            elif args:
                branch.add(Text(str(args), style="default"))
        return tree

    @staticmethod
    def _format_scalar(value: Any, max_length: int = 160) -> str:
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
    def _add_tree_value(branch: Tree, value: Any, key: str | None = None) -> None:
        if isinstance(value, Mapping):
            target = branch.add(f"[dim]{key}[/dim]") if key is not None else branch
            for child_key, child_value in value.items():
                StreamRenderer._add_tree_value(target, child_value, str(child_key))
            return
        if isinstance(value, list):
            target = branch.add(f"[dim]{key}[/dim]") if key is not None else branch
            for idx, item in enumerate(value):
                StreamRenderer._add_tree_value(target, item, f"[{idx}]")
            return
        formatted = StreamRenderer._format_scalar(value)
        if key is None:
            branch.add(Text(formatted, style="default"))
        else:
            branch.add(Text.assemble((f"{key}: ", "dim"), (formatted, "default")))

    @staticmethod
    def _extract_tool_content(data: Mapping[str, Any]) -> str:
        content = data.get("content")
        if content:
            return str(content)
        output = data.get("tool_output")
        if isinstance(output, Mapping):
            nested_content = output.get("content")
            if nested_content is not None:
                return str(nested_content)
            nested_data = output.get("data")
            if isinstance(nested_data, Mapping) and nested_data.get("content") is not None:
                return str(nested_data.get("content"))
        return ""

    def start(self) -> None:
        """Start the waiting spinner."""
        if not RICH_AVAILABLE:
            return
        self._stop_status()
        self._status = Live(
            self._build_waiting_panel(),
            console=self._console,
            transient=True,
            refresh_per_second=DEFAULT_LIVE_REFRESH_PER_SECOND,
        )
        self._status.start()
        self._phase = "waiting"

    def stop(self) -> None:
        """Stop any live rendering."""
        self._stop_status()

    def handle_event(self, data: Mapping[str, Any]) -> None:
        """Render one Jiuwen-style stream event from ``DataAgent.astream``."""
        msg_type = str(data.get("type", ""))
        if msg_type == "llm_output":
            self.render_output(str(self._extract_content(data)))
            return
        if msg_type == "answer":
            if not self._has_llm_output:
                self.render_output(str(self._extract_content(data)))
            return
        if msg_type == "llm_reasoning":
            self.render_reasoning(str(self._extract_content(data)))
            return
        if msg_type == "__interaction__":
            self.render_interaction(data)
            return
        if msg_type == "interaction":
            self.render_interaction(data)
            return
        if msg_type == "tool_call":
            self.render_tool_call(data)
            return
        if msg_type == "tool_result":
            self.render_tool_result(data)
            return
        if msg_type == "tool_status":
            self.render_tool_status(data)
            return
        if msg_type in {"planner_tool_calls", "tool_calls"}:
            tool_calls = data.get("tool_calls", [])
            if isinstance(tool_calls, list):
                self.render_tool_calls([item for item in tool_calls if isinstance(item, dict)])
            return
        content = self._extract_content(data)
        if content:
            self.render_output(content)

    def render_output(self, content: str) -> None:
        """Render Jiuwen ``llm_output`` tokens in a Rich Markdown panel."""
        if not content:
            return
        if self._phase == "reasoning":
            self._reasoning_content = ""
        self._has_llm_output = True
        self._has_rendered_content = True
        self._has_rendered_event = True
        if not RICH_AVAILABLE:
            self._console.print(content, end="")
            return
        if self._phase != "output":
            self._stop_status()
            self._stream_content = ""
            self._phase = "output"
        self._stream_content += content
        if self._status is None:
            self._status = Live(
                self._build_stream_panel(self._stream_content),
                console=self._console,
                transient=False,
                refresh_per_second=DEFAULT_LIVE_REFRESH_PER_SECOND,
            )
            self._status.start()
            return
        self._status.update(self._build_stream_panel(self._stream_content), refresh=True)

    def render_reasoning(self, content: str) -> None:
        """Render streamed model reasoning separately from assistant output."""
        if not self._show_reasoning:
            return
        if not content.strip():
            return
        if self._phase != "reasoning":
            self._reasoning_content = ""
        if content.startswith(self._reasoning_content):
            self._reasoning_content = content
        else:
            self._reasoning_content += content
        self._has_rendered_event = True
        if not RICH_AVAILABLE:
            return
        if self._phase != "reasoning":
            self._stop_status()
            self._phase = "reasoning"
        if self._status is None:
            self._status = Live(
                self._build_reasoning_panel(self._reasoning_content),
                console=self._console,
                transient=False,
                refresh_per_second=DEFAULT_LIVE_REFRESH_PER_SECOND,
            )
            self._status.start()
            return
        self._status.update(self._build_reasoning_panel(self._reasoning_content), refresh=True)

    def render_interaction(self, data: Mapping[str, Any]) -> None:
        """Render a human interaction request from Jiuwen HITL."""
        self._stop_status()
        self._phase = "interaction"
        payload = data.get("payload", {})
        payload = payload if isinstance(payload, Mapping) else data
        questions = payload.get("questions", data.get("questions", []))
        lines: list[str] = []
        if isinstance(questions, list) and questions:
            for question in questions:
                if isinstance(question, Mapping):
                    lines.append(f"- {question.get('question', '请提供反馈')}")
        else:
            lines.append(str(payload.get("message") or data.get("message") or "请提供反馈"))
        body = "\n".join(lines)
        if not RICH_AVAILABLE:
            self._console.print(body)
            self._has_rendered_event = True
            return
        title = "[bold yellow]需要人工反馈[/bold yellow]"
        self._console.print(Panel(Markdown(body), title=title, border_style="yellow", padding=(1, 2)))
        self._has_rendered_event = True

    def render_tool_call(self, data: Mapping[str, Any]) -> None:
        """Render a Jiuwen ``tool_call`` event."""
        self._stop_status()
        self._phase = "tool"
        payload = data.get("payload", {})
        payload = payload if isinstance(payload, Mapping) else {}
        tool_name = str(payload.get("tool_name") or "unknown")
        tool_args = payload.get("tool_args", {})
        if not RICH_AVAILABLE:
            self._console.print(f"{tool_name}: 执行中")
            self._has_rendered_event = True
            return
        grid = Table.grid(padding=(0, 1))
        grid.add_row(Spinner("dots", style="yellow"), Text("执行中...", style="yellow"))
        renderables: list[Any] = [grid]
        if isinstance(tool_args, Mapping) and tool_args:
            args_tree = Tree("[dim]args[/dim]")
            self._add_tree_value(args_tree, tool_args)
            renderables.append(args_tree)
        self._console.print(
            Panel(
                Group(*renderables),
                title=f"[bold yellow]调用工具: {tool_name}[/bold yellow]",
                border_style="yellow",
                padding=(1, 2),
            )
        )
        self._has_rendered_event = True

    def render_tool_result(self, data: Mapping[str, Any]) -> None:
        """Render a Jiuwen ``tool_result`` event."""
        self._stop_status()
        self._phase = "tool"
        payload = data.get("payload", {})
        payload = payload if isinstance(payload, Mapping) else {}
        tool_name = str(payload.get("tool_name") or "unknown")
        tool_result = str(payload.get("tool_result") or "")
        error = str(payload.get("error") or "")
        status = str(payload.get("status") or "")
        is_error = bool(error) or status == "error"
        if not RICH_AVAILABLE:
            self._console.print(f"{tool_name}: {'失败' if is_error else '完成'}")
            if error or tool_result:
                self._console.print(error or tool_result)
            self._has_rendered_event = True
            return
        body = error or tool_result or "工具执行完成"
        if len(body) > DEFAULT_RICH_RESULT_TRUNCATION:
            body = f"{body[:DEFAULT_RICH_RESULT_TRUNCATION]}\n\n... (输出已截断)"
        border_style = "red" if is_error else "green"
        title_style = "red" if is_error else "green"
        title_label = "工具失败" if is_error else "工具完成"
        self._console.print(
            Panel(
                Markdown(prepare_model_content_for_markdown(body)),
                title=f"[bold {title_style}]{title_label}: {tool_name}[/bold {title_style}]",
                border_style=border_style,
                padding=(1, 2),
            )
        )
        self._has_rendered_event = True

    def render_tool_calls(self, tool_calls: list[dict[str, Any]]) -> None:
        """Render structured tool-call events."""
        if not tool_calls:
            return
        self._stop_status()
        self._phase = "tool"
        if not RICH_AVAILABLE:
            self._console.print("\n".join(str(tool_call.get("name", "unknown")) for tool_call in tool_calls))
            self._has_rendered_event = True
            return
        self._console.print()
        self._console.print(self._build_tool_tree(tool_calls))
        self._console.print()
        self._has_rendered_event = True

    def render_tool_status(self, data: Mapping[str, Any]) -> None:
        """Render Jiuwen tracer tool status events outside the assistant answer."""
        tool_name = str(data.get("tool_name") or "unknown")
        status = str(data.get("status") or "running")
        content = self._extract_tool_content(data)
        error = str(data.get("error") or "")
        tool_args = data.get("tool_args", {})
        if not RICH_AVAILABLE:
            label = "完成" if status == "success" else "失败" if status == "error" else "执行中"
            self._console.print(f"{tool_name}: {label}")
            if content:
                self._console.print(content)
            return

        self._stop_status()
        self._phase = "tool"
        if status in {"running", "start"}:
            grid = Table.grid(padding=(0, 1))
            grid.add_row(Spinner("dots", style="yellow"), Text("执行中...", style="yellow"))
            renderables: list[Any] = [grid]
            if isinstance(tool_args, Mapping) and tool_args:
                args_tree = Tree("[dim]args[/dim]")
                self._add_tree_value(args_tree, tool_args)
                renderables.append(args_tree)
            border_style = "yellow"
            title = f"[bold yellow]调用工具: {tool_name}[/bold yellow]"
        elif status == "error":
            error_content = error or content or "工具执行失败"
            renderables = [Markdown(prepare_model_content_for_markdown(error_content))]
            border_style = "red"
            title = f"[bold red]工具失败: {tool_name}[/bold red]"
        else:
            if content:
                if len(content) > DEFAULT_RICH_RESULT_TRUNCATION:
                    content = f"{content[:DEFAULT_RICH_RESULT_TRUNCATION]}\n\n... (输出已截断)"
                renderables = [Markdown(prepare_model_content_for_markdown(content))]
            else:
                renderables = [Text("工具执行完成", style="green")]
            border_style = "green"
            title = f"[bold green]工具完成: {tool_name}[/bold green]"

        self._console.print(Panel(Group(*renderables), title=title, border_style=border_style, padding=(1, 2)))
        self._has_rendered_event = True

    def render_final_result(self, response: Mapping[str, Any]) -> None:
        """Render a non-streamed final result if no streamed assistant content was shown."""
        if self._has_rendered_content:
            return
        content = self._final_content(response)
        if not content:
            return
        if len(content) > DEFAULT_RICH_RESULT_TRUNCATION:
            content = f"{content[:DEFAULT_RICH_RESULT_TRUNCATION]}\n\n... (输出已截断)"
        if not RICH_AVAILABLE:
            self._console.print(content)
            return
        self._console.print(
            Panel(
                Markdown(prepare_model_content_for_markdown(content)),
                title="[bold cyan]🤖 Agent[/bold cyan]",
                border_style="cyan",
                padding=(1, 2),
            )
        )
        self._console.print()

    def render_error(self, message: str) -> None:
        """Render an error panel."""
        self._stop_status()
        if not RICH_AVAILABLE:
            self._console.print(str(message))
            return
        self._console.print(Panel(str(message), title="[bold red]❌ 错误[/bold red]", border_style="red", padding=(1, 2)))

    def _stop_status(self) -> None:
        if self._status is None:
            return
        try:
            self._status.stop()
        except (RuntimeError, OSError):
            logger.warning("Failed to stop Rich live status", exc_info=True)
        self._status = None

    def _final_content(self, response: Mapping[str, Any]) -> str:
        final_answer = response.get("final_answer")
        if final_answer:
            return str(final_answer)
        messages = response.get("messages", [])
        if not isinstance(messages, list) or not messages:
            return ""
        last_message = messages[-1]
        if isinstance(last_message, Mapping):
            return str(last_message.get("content") or last_message.get("output") or last_message)
        return str(last_message)
