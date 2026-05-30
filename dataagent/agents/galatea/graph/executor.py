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
from copy import deepcopy

from langchain_core.messages import ToolMessage

from dataagent.agents.galatea.state.state import State
from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.cbb.runtime import Runtime

CREATE_SUBAGENT_TOOL = "create_subagent"

API_PARAMS = {
    "tavily_config": "tavily_configs",
    "llm_config": "llm_configs",
}


class ExecutorNode(BaseNode):
    def __init__(self):
        super().__init__(name="executor", pre_hooks=[], post_hooks=[])

    @staticmethod
    def _truncate(value, *, limit: int):
        text = str(value if value is not None else "")
        if len(text) <= limit:
            return value if value is not None else {}
        tail = (
            f"...(truncated: showing first {limit} chars out of {len(text)} chars. "
            "Reason: very large tool outputs are capped before being returned to the model, "
            "so they do not flood context or degrade reasoning quality. "
            "If the missing content matters, do not request the same full dump again. "
            "Prefer targeted retrieval instead: rerun the underlying command or query with "
            "command-side filtering so it returns only the specific field, row, match, block, "
            "or section you need, or use `bash` to inspect only the needed portion directly, "
            "for example with `head`, `tail`, `rg`, `sed`, or `jq`.)"
        )
        return text[:limit] + "\n\n" + tail

    @staticmethod
    def _execute_tool_call(*, action_manager, runtime: Runtime, state: State, tool_name: str, tool_args: dict) -> dict:
        if tool_name == CREATE_SUBAGENT_TOOL:
            tool_args["user_id"] = state["user_id"]
            tool_args["env"] = runtime.env

        tool = action_manager.get_tool(tool_name)
        tool_params = tool.__code__.co_varnames
        for api_param_key, api_param_value in API_PARAMS.items():
            if api_param_key in tool_params:
                tool_args[api_param_key] = getattr(runtime.env, api_param_value).get(tool_name, "")

        return action_manager.call(tool_name, tool_args)

    def _process(self, state: State, runtime: Runtime) -> State:
        runtime.ensure_not_cancelled()
        updated_state = deepcopy(state)

        if "action_manager" not in self.modules:
            raise ValueError("Register ActionManager before calling the ExecutorNode")

        action_manager = self.get_module("action_manager")

        message = state["messages"][-1]
        tool_calls = message.tool_calls or []
        invalid_tool_calls = getattr(message, "invalid_tool_calls", None) or []

        for tool_call in tool_calls:
            runtime.ensure_not_cancelled()
            tool_name = tool_call.get("name")
            tool_args = tool_call.get("args") or {}
            tool_call_id = tool_call.get("id")
            result = self._execute_tool_call(
                action_manager=action_manager,
                runtime=runtime,
                state=state,
                tool_name=tool_name,
                tool_args=tool_args,
            )
            updated_state["messages"].append(ToolMessage(content=result, tool_call_id=tool_call_id))

        for index, invalid_call in enumerate(invalid_tool_calls):
            runtime.ensure_not_cancelled()
            recovered_result, recovered_id = self._handle_invalid_tool_call(
                invalid_call=invalid_call,
                fallback_index=index,
            )
            updated_state["messages"].append(ToolMessage(content=recovered_result, tool_call_id=recovered_id))

        return updated_state

    def _handle_invalid_tool_call(self, *, invalid_call: dict, fallback_index: int) -> tuple[dict, str]:
        tool_name = str(invalid_call.get("name") or "")
        tool_call_id = str(invalid_call.get("id") or f"invalid_tool_call_{fallback_index}")
        raw_args = invalid_call.get("args")
        raw_error = str(invalid_call.get("error") or "").strip()

        error_hint = (
            "Tool call arguments could not be parsed by LangChain before execution. "
            "Retry with smaller payloads: use write + multiple edit calls, and ensure JSON escaping is valid."
        )
        if raw_error:
            error_hint = f"{error_hint} Parser error: {self._truncate(raw_error, limit=300)}"

        return (
            {
                "status": "ERROR",
                "tool_name": tool_name or "unknown_tool",
                "tool_args": self._truncate(raw_args, limit=600),
                "error": error_hint,
            },
            tool_call_id,
        )
