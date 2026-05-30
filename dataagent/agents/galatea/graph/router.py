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
import ast
import sys
import time
from pathlib import Path
from typing import cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from wcwidth import wcswidth

from dataagent.agents.galatea.hooks.streamer import _content_to_text
from dataagent.agents.galatea.state.state import State
from dataagent.agents.galatea.utils.history_utils import append_history_messages
from dataagent.core.cbb.base_router import BaseRouter
from dataagent.core.cbb.base_state import BaseState
from dataagent.core.cbb.runtime import Runtime


class GalateaRouter(BaseRouter):
    def __init__(self, entry: str):
        super().__init__(entry)

    @staticmethod
    def _stream_print(content: str) -> None:
        for char in content:
            sys.stdout.write(char)
            sys.stdout.flush()
            if char not in {" ", "\n"}:
                time.sleep(0.02)
        sys.stdout.write("\n")
        sys.stdout.flush()

    def process(self, curr_node: str, state: BaseState, runtime: object = None) -> str:
        """Process."""
        gs = cast(State, state)
        rt = cast(Runtime | None, runtime)
        gs["curr_iter"] = int(gs.get("curr_iter", 0)) + 1
        max_iter = int(getattr(rt, "max_iter", 100) if rt is not None else 100)
        if gs["curr_iter"] > max_iter:
            return "__end__"

        if len(gs["messages"]) == 3:
            self._print_message(
                HumanMessage(content=gs["user_query"]),
                gs["enable_hierarchical_orchestration"],
                gs["hierarchy"],
            )
        self._print_message(
            gs["messages"][-1],
            gs["enable_hierarchical_orchestration"],
            gs["hierarchy"],
        )

        new_messages: list[BaseMessage] = [gs["messages"][-1]]
        if int(gs.get("curr_iter", 0)) == 1:
            new_messages = [HumanMessage(content=str(gs.get("user_query", ""))), *new_messages]
        append_history_messages(
            workspace_dir=Path(getattr(rt.env, "workspace_dir", Path.cwd())) if rt is not None else Path.cwd(),
            messages=new_messages,
        )

        rules = self.rules
        if curr_node in rules:
            return rules[curr_node](gs)

        return "__end__"

    def _print_message(
        self,
        message: BaseMessage,
        enable_hierarchical_orchestration: bool | None = False,
        hierarchy: str | None = "",
    ) -> None:
        hierarchy_label = hierarchy or ""
        if enable_hierarchical_orchestration or hierarchy_label.upper() != "MAIN":
            source = hierarchy_label.upper() + " - " + type(message).__name__
        else:
            source = type(message).__name__

        if isinstance(message, HumanMessage):
            content = _content_to_text(message.content)
        elif isinstance(message, AIMessage):
            content = _content_to_text(message.content).strip()
            tool_calls = message.tool_calls or []
            for tool_call in tool_calls:
                tool_name = tool_call.get("name")
                tool_args = tool_call.get("args") or {}
                content += f"\n\nTool: {tool_name}\n    Args: {tool_args}\n"
        elif isinstance(message, ToolMessage):
            tool_body = message.content if isinstance(message.content, str) else str(message.content)
            content_dict = ast.literal_eval(tool_body)
            content = str(content_dict.get("result"))
            content = content.replace("\\r\\n", "\n").replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")
        else:
            content = str(message)

        width = 120
        top = "╔" + "═" * (width - 2) + "╗"
        bottom = "╚" + "═" * (width - 2) + "╝"
        side = "║"

        header = side + f"  {source}  ".center(width - 2).rstrip()
        header += " " * (width - 1 - len(header)) + side

        body_lines = []
        curr_line = side + " "
        for char in content:
            if char == "\n":
                curr_line += " " * (width - 1 - wcswidth(curr_line)) + side
                body_lines.append(curr_line)
                curr_line = side + " "
            elif wcswidth(curr_line + char) > width - 2:
                curr_line += " " * (width - 1 - wcswidth(curr_line)) + side
                body_lines.append(curr_line)
                curr_line = side + " " + char
            else:
                curr_line += char
        if curr_line:
            curr_line += " " * (width - 1 - wcswidth(curr_line)) + side
            body_lines.append(curr_line)

        print()
        print(top)
        print(header)
        print(side + "═" * (width - 2) + side)
        for bl in body_lines:
            print(bl)
        print(bottom)
