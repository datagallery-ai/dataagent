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
# 本文件中的模型可见文案原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""用 `task` 把任务交给子代理。

声明式 spec 在这里编译成 runnable；已经编译好的 runnable 原样挂上名字。
`mode="fork"` 时子代理接着父对话做，其它模式只看到这一次的任务描述。
"""

import contextlib
import dataclasses
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Annotated, Any, Literal, NotRequired, TypeAlias, TypedDict

from dataagent.core.middleware._utils import append_to_system_message
from dataagent.core.middleware.summarization import (
    SUMMARIZATION_EVENT_KEY,
    SUMMARIZATION_SESSION_ID_KEY,
    SummarizationEvent,
    _DeepAgentsSummarizationMiddleware,
)
from dataagent.core.middleware.filesystem.filesystem import FilesystemMiddleware, FilesystemPermission
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, InterruptOnConfig
from langchain.agents.middleware import types as mw_types
from langchain.agents.structured_output import ResponseFormat
from langchain.tools import ToolRuntime
from langchain_core._api.beta_decorator import warn_beta
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Command
from langsmith.run_helpers import get_tracing_context, tracing_context
from pydantic import BaseModel, Field

from dataagent.core.backends.protocol import BackendProtocol

AgentMiddleware = mw_types.AgentMiddleware
ContextT = mw_types.ContextT
ModelRequest = mw_types.ModelRequest
ModelResponse = mw_types.ModelResponse
OmitFromSchema = mw_types.OmitFromSchema
ResponseT = mw_types.ResponseT
TracePolicy = mw_types.TracePolicy
omit_payload = mw_types.omit_payload

ToolLike: TypeAlias = BaseTool | Callable[..., Any] | dict[str, Any]
ModelChoice: TypeAlias = str | BaseChatModel
ReplyFormat: TypeAlias = ResponseFormat[Any] | type | dict[str, Any]
InterruptMap: TypeAlias = dict[str, bool | InterruptOnConfig]
AgentSpec: TypeAlias = "SubAgent | CompiledSubAgent"
_NO_KEYS: frozenset[str] = frozenset()

SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY = "__deepagents_subagent_response_format"
_FORKED_CONTEXT_KEY = "_deepagents_forked_context"
_FORK_RECURSION_REFUSAL = "You are a subagent and cannot delegate to another subagent. Complete this task yourself instead of calling this tool again."
_FORK_TASK_PREAMBLE = (
    "[The messages above are a prior conversation you are continuing as the "
    "subagent that was just invoked. Any mention in them of delegating to a "
    "subagent already happened — you are that subagent, not the one being "
    "asked to delegate further. If you try to delegate to another subagent "
    "yourself, it will be refused — complete this task directly. Use the "
    "specific facts, figures, and identifiers already established in that "
    "conversation when completing the task below — do not answer "
    "generically when exact details are already available above. Your "
    "actual task is below.]\n\n"
)
_FORKED_SUBAGENT_TOOL_NOTE = " (inherits your full conversation and system prompt — no need to restate context here)"
_AVAILABLE_HEADING = "\n\nAvailable subagent types:\n\n"
_MISSING_MESSAGES = (
    "CompiledSubAgent must return a state containing a 'messages' key. "
    "Custom StateGraphs used with CompiledSubAgent should include 'messages' "
    "in their state schema to communicate results back to the main agent."
)
_TASK_DESCRIPTION_HELP = (
    "A detailed description of the task for the subagent to perform autonomously. "
    "Include all necessary context and specify the expected output format."
)
_TASK_TYPE_HELP = "The type of subagent to use. Must be one of the available agent types listed in the tool description."

DEFAULT_SUBAGENT_PROMPT = """In order to complete the objective that the user asks of you, you have access to a number of standard tools.

The calling agent only sees your final assistant message, not your intermediate work, tool results, or status tracking. Ensure your final
response contains the complete answer."""

TASK_TOOL_DESCRIPTION = """Launch an ephemeral subagent to handle a complex, multi-step task.

Available agent types and the tools they have access to:
{available_agents}

Specify subagent_type to select the agent. Usage notes:
- Launch multiple agents concurrently when their tasks are independent, using a single message with multiple tool calls.
- Each invocation is stateless by default: the agent sees only the prompt you give it and returns a single final report. Put full detail in the prompt and state exactly what it should return — unless an agent type below says it inherits your conversation instead.
- The agent's report is not shown to the user; relay a summary yourself.
- Tell the agent whether to create content, analyze, or only research, since it can't necessarily see the user's intent unless it inherits your conversation, as noted per agent type below.
- If an agent's description says to use it proactively, do so without waiting to be asked.
- When only general-purpose is available, use it for any complex, context-heavy task; it has the same capabilities as the main agent."""

DEFAULT_GENERAL_PURPOSE_DESCRIPTION = "General-purpose agent for researching complex questions, searching for files and content, and executing multi-step tasks. When you are searching for a keyword or file and are not confident that you will find the right match in the first few tries use this agent to perform the search for you. This agent has access to all tools as the main agent."

# 声明式 fork 不把这三键交给子图。摘要事件先折进消息，会话号留给子图自己生成。
_FORK_DROP = frozenset({"structured_response", SUMMARIZATION_EVENT_KEY, SUMMARIZATION_SESSION_ID_KEY})
# 隔离输入、编译型 fork 输入，以及子图返回父级时丢掉这些键。
# skills_metadata 留下。已安装的 deepagents 0.7.15（988 行）不丢这个键。
_ISOLATED_DROP = frozenset({"messages", "todos", "structured_response", _FORKED_CONTEXT_KEY})
_ACCEPTED_MODES = (None, "isolated", "fork", "handoff")


class SubAgent(TypedDict):
    """声明式子代理。默认只看到本次任务；`mode="fork"` 才继承父对话。"""

    name: str
    description: str
    tools: NotRequired[Sequence[ToolLike]]
    model: NotRequired[ModelChoice]
    middleware: NotRequired[list[AgentMiddleware]]
    interrupt_on: NotRequired[InterruptMap]
    skills: NotRequired[list[str]]
    permissions: NotRequired[list[FilesystemPermission]]
    response_format: NotRequired[ReplyFormat]
    system_prompt: NotRequired[str]
    mode: NotRequired[Literal["isolated", "fork"]]


class CompiledSubAgent(TypedDict):
    """调用方已经编译好的 runnable。state 里必须能回到 `messages`。"""

    name: str
    description: str
    runnable: Runnable
    mode: NotRequired[Literal["isolated", "fork"]]


class TaskToolSchema(BaseModel):
    """`task` 的参数表。`runtime` 由框架注入，不出现在这里。"""

    description: str = Field(description=_TASK_DESCRIPTION_HELP)
    subagent_type: str = Field(description=_TASK_TYPE_HELP)


GENERAL_PURPOSE_SUBAGENT: SubAgent = {
    "name": "general-purpose",
    "description": DEFAULT_GENERAL_PURPOSE_DESCRIPTION,
    "system_prompt": DEFAULT_SUBAGENT_PROMPT,
}


_FORK_MARK = NotRequired[Annotated[bool, OmitFromSchema(input=False, output=True)]]


class _ForkedContextState(TypedDict):
    """让 fork 子图真的跟踪 `_deepagents_forked_context` 这个通道。"""

    _deepagents_forked_context: _FORK_MARK


class _ForkTaskToolMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    """把父级的 `task` 原样挂进 fork，递归调用时由 state 标记拒绝。"""

    state_schema = _ForkedContextState

    def __init__(self, task_tool: BaseTool) -> None:
        self.tools = [task_tool]


def _is_compiled_subagent(spec: Mapping[str, Any]) -> bool:
    return "runnable" in spec


def _is_forked_subagent(spec: Mapping[str, Any]) -> bool:
    mode = spec.get("mode")
    return mode == "fork" and not _is_compiled_subagent(spec)


def _is_forked_compiled_subagent(spec: Mapping[str, Any]) -> bool:
    return _is_compiled_subagent(spec) and spec.get("mode") == "fork"


def _continues_parent(spec: Mapping[str, Any]) -> bool:
    return _is_forked_subagent(spec) or _is_forked_compiled_subagent(spec)


def _reject_duplicate_names(specs: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for spec in specs:
        name = spec["name"]
        if name in seen:
            raise ValueError(f"Duplicate subagent name '{name}'; each subagent must have a unique name.")
        seen.add(name)


def _reject_mode(spec: Mapping[str, Any]) -> None:
    mode = spec.get("mode")
    if mode not in _ACCEPTED_MODES:
        raise ValueError(f"SubAgent '{spec['name']}' has invalid mode '{mode}'; expected 'isolated' or 'fork'")
    if mode == "fork" and spec.get("skills"):
        raise ValueError(f"SubAgent '{spec['name']}' cannot set skills under mode='fork'; the parent's skills are inherited instead.")


def _listing_line(agent_name: str, summary: str, *, continues: bool) -> str:
    note = _FORKED_SUBAGENT_TOOL_NOTE if continues else ""
    return f"- {agent_name}: {summary}{note}"


def _join_listings(specs: Sequence[Mapping[str, Any]]) -> str:
    lines = [_listing_line(spec["name"], spec["description"], continues=_continues_parent(spec)) for spec in specs]
    return "\n".join(lines)


def _choose_description(specs: Sequence[Mapping[str, Any]], override: str | None) -> str:
    listing = _join_listings(specs)
    if override is None:
        return TASK_TOOL_DESCRIPTION.format(available_agents=listing)
    if "{available_agents}" in override:
        return override.format(available_agents=listing)
    return override


def _compose_system_prompt(system_prompt: str | None, specs: Sequence[Mapping[str, Any]]) -> str | None:
    if not system_prompt or not specs:
        return system_prompt
    return system_prompt + _AVAILABLE_HEADING + _join_listings(specs)


def _copy_except(source: Mapping[str, Any], blocked: frozenset[str]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key, value in source.items():
        if key in blocked:
            continue
        kept[key] = value
    return kept


def _history_for_fork(messages: Sequence[Any], event: SummarizationEvent | None, description: str) -> list[Any]:
    history = list(messages)
    if history:
        last = history[-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            history.pop()
    folded = _DeepAgentsSummarizationMiddleware._apply_event_to_messages(history, event)
    return [*folded, HumanMessage(content=_FORK_TASK_PREAMBLE + description)]


def _child_state(spec: Mapping[str, Any], runtime: ToolRuntime, description: str, private_keys: frozenset[str]) -> dict[str, Any]:
    parent = runtime.state
    if _continues_parent(spec):
        if _is_forked_subagent(spec):
            carried = _copy_except(parent, _FORK_DROP)
            carried[_FORKED_CONTEXT_KEY] = True
        else:
            carried = _copy_except(parent, _ISOLATED_DROP | private_keys)
        carried["messages"] = _history_for_fork(parent.get("messages", []), parent.get(SUMMARIZATION_EVENT_KEY), description)
        return carried
    carried = _copy_except(parent, _ISOLATED_DROP | private_keys)
    carried["messages"] = [HumanMessage(content=description)]
    return carried


def _structured_text(structured: Any) -> str:
    if hasattr(structured, "model_dump_json"):
        return structured.model_dump_json()
    if dataclasses.is_dataclass(structured) and not isinstance(structured, type):
        return json.dumps(dataclasses.asdict(structured))
    return json.dumps(structured)


def _assistant_text(messages: Sequence[Any]) -> str:
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        raw = message.text if message.text else ""
        trimmed = raw.rstrip()
        if trimmed:
            return trimmed
    return ""


def _command_from_result(result: Mapping[str, Any], tool_call_id: str, private_keys: frozenset[str]) -> Command:
    if "messages" not in result:
        raise ValueError(_MISSING_MESSAGES)
    update = _copy_except(result, _ISOLATED_DROP | private_keys)
    structured = result.get("structured_response")
    content = _structured_text(structured) if structured is not None else _assistant_text(result["messages"])
    update["messages"] = [ToolMessage(content, tool_call_id=tool_call_id)]
    return Command(update=update)


def _dynamic_schema(runtime: ToolRuntime) -> Any:
    config = runtime.config
    configurable = config.get("configurable") if isinstance(config, dict) else None
    if isinstance(configurable, dict):
        return configurable.get(SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY)
    return None


def _blocked_reply(subagent_type: str, runtime: ToolRuntime, graphs: Mapping[str, Runnable]) -> str | None:
    if runtime.state.get(_FORKED_CONTEXT_KEY):
        return _FORK_RECURSION_REFUSAL
    if subagent_type not in graphs:
        allowed = ", ".join(f"`{name}`" for name in graphs)
        return f"We cannot invoke subagent {subagent_type} because it does not exist, the only allowed types are {allowed}"
    if not runtime.tool_call_id:
        raise ValueError("Tool call ID is required for subagent invocation")
    return None


@contextlib.contextmanager
def _subagent_trace():
    current = get_tracing_context()
    metadata = {**(current.get("metadata") or {}), "ls_agent_type": "subagent"}
    with tracing_context(**{**current, "metadata": metadata}):
        yield


def _run_config() -> dict[str, Any]:
    return {"configurable": {"ls_agent_type": "subagent"}}


def create_sub_agent(spec: SubAgent, *, state_schema: type | None = None, response_format: ReplyFormat | None = None) -> Runnable:
    """把一条声明式 spec 编成 runnable。`model` 和 `tools` 必须在 spec 里。"""
    if "model" not in spec:
        raise ValueError(f"SubAgent '{spec['name']}' must specify 'model'")
    if "tools" not in spec:
        raise ValueError(f"SubAgent '{spec['name']}' must specify 'tools'")
    from dataagent.core._models import resolve_model

    model = resolve_model(spec["model"])
    middleware = list(spec.get("middleware", []))
    interrupt_on = spec.get("interrupt_on")
    if interrupt_on:
        middleware.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on))
    selected = spec.get("response_format") if response_format is None else response_format
    kwargs: dict[str, Any] = {
        "system_prompt": spec.get("system_prompt", ""),
        "tools": spec["tools"],
        "middleware": middleware,
        "name": spec["name"],
        "response_format": selected,
    }
    if state_schema is not None:
        kwargs["state_schema"] = state_schema
    return create_agent(model, **kwargs)


def _mirror_task_tool(func: Callable[..., Any], coroutine: Callable[..., Any], description: str) -> BaseTool:
    return StructuredTool.from_function(
        name="task",
        func=func,
        coroutine=coroutine,
        description=description,
        infer_schema=False,
        args_schema=TaskToolSchema,
    )


def _with_fork_guard(spec: Mapping[str, Any], func: Callable[..., Any], coroutine: Callable[..., Any], description: str) -> Mapping[str, Any]:
    if not _is_forked_subagent(spec):
        return spec
    resolved = {key: value for key, value in spec.items() if key != "mode"}
    stack = list(spec.get("middleware", []))
    insert_at = 0
    for index, item in enumerate(stack):
        if isinstance(item, FilesystemMiddleware):
            insert_at = index + 1
            break
    stack.insert(insert_at, _ForkTaskToolMiddleware(_mirror_task_tool(func, coroutine, description)))
    resolved["middleware"] = stack
    return resolved


def _compile_one(
    spec: Mapping[str, Any],
    func: Callable[..., Any],
    coroutine: Callable[..., Any],
    description: str,
    *,
    state_schema: type | None,
    response_format: Any,
) -> dict[str, Any]:
    if "runnable" in spec:
        if response_format is not None:
            raise ValueError(f'response_schema cannot be used with compiled subagent "{spec["name"]}"; dynamic schemas require a raw SubAgent spec.')
        runnable = spec["runnable"].with_config({"metadata": {"lc_agent_name": spec["name"]}, "run_name": spec["name"]})
        return {"name": spec["name"], "description": spec["description"], "runnable": runnable}
    prepared = _with_fork_guard(spec, func, coroutine, description)
    runnable = create_sub_agent(prepared, state_schema=state_schema, response_format=response_format)
    return {"name": spec["name"], "description": spec["description"], "runnable": runnable}


def _select_runnable(spec: Mapping[str, Any], runtime: ToolRuntime, graphs: Mapping[str, Runnable], compile_args: dict[str, Any]) -> Runnable:
    schema = _dynamic_schema(runtime)
    if schema is None:
        return graphs[spec["name"]]
    compiled = _compile_one(spec, response_format=schema, **compile_args)
    return compiled["runnable"]


def _build_task_tool(
    subagents: Sequence[AgentSpec],
    task_description: str | None = None,
    *,
    private_state_keys: frozenset[str] = _NO_KEYS,
    state_schema: type | None = None,
) -> BaseTool:
    """按 spec 列表做一把 `task`。重名和非法 mode 在编译前就拒绝。"""
    _reject_duplicate_names(subagents)
    for spec in subagents:
        _reject_mode(spec)
    tool_description = _choose_description(subagents, task_description)
    by_name = {spec["name"]: spec for spec in subagents}

    def task(description: str, subagent_type: str, runtime: ToolRuntime) -> str | Command:
        blocked = _blocked_reply(subagent_type, runtime, graphs)
        if blocked is not None:
            return blocked
        spec = by_name[subagent_type]
        runnable = _select_runnable(spec, runtime, graphs, compile_args)
        state = _child_state(spec, runtime, description, private_state_keys)
        with _subagent_trace():
            result = runnable.invoke(state, _run_config())
        return _command_from_result(result, runtime.tool_call_id, private_state_keys)

    async def atask(description: str, subagent_type: str, runtime: ToolRuntime) -> str | Command:
        blocked = _blocked_reply(subagent_type, runtime, graphs)
        if blocked is not None:
            return blocked
        spec = by_name[subagent_type]
        runnable = _select_runnable(spec, runtime, graphs, compile_args)
        state = _child_state(spec, runtime, description, private_state_keys)
        with _subagent_trace():
            result = await runnable.ainvoke(state, _run_config())
        return _command_from_result(result, runtime.tool_call_id, private_state_keys)

    compile_args = {"func": task, "coroutine": atask, "description": tool_description, "state_schema": state_schema}
    compiled = [_compile_one(spec, response_format=None, **compile_args) for spec in subagents]
    graphs = {item["name"]: item["runnable"] for item in compiled}
    return _mirror_task_tool(task, atask, tool_description)


class SubAgentMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    """给主代理挂上 `task`。`backend` 只保存，本文件不调用它。"""

    trace_policy = TracePolicy(process_inputs=omit_payload)

    def __init__(self, *, backend: BackendProtocol, subagents: Sequence[SubAgent | CompiledSubAgent], system_prompt: str | None = None, task_description: str | None = None, private_state_keys: frozenset[str] | None = None, state_schema: type | None = None) -> None:
        super().__init__()
        if not subagents:
            raise ValueError("At least one subagent must be specified")
        self._backend = backend
        self._subagents = subagents
        self._private_state_keys = private_state_keys or _NO_KEYS
        self._task_description = task_description
        self._state_schema = state_schema
        if any(_continues_parent(spec) for spec in subagents):
            warn_beta(name="forked subagents", obj_type="feature")
        self.subagent_names = frozenset(spec["name"] for spec in subagents)
        self.system_prompt = _compose_system_prompt(system_prompt, subagents)
        self.tools = [self._task_tool()]

    def _task_tool(self) -> BaseTool:
        return _build_task_tool(self._subagents, task_description=self._task_description, private_state_keys=self._private_state_keys, state_schema=self._state_schema)

    def _read_private_keys(self) -> frozenset[str]:
        return self._private_state_keys

    def _write_private_keys(self, value: frozenset[str]) -> None:
        self._private_state_keys = value
        self.tools = [self._task_tool()]

    private_state_keys = property(_read_private_keys, _write_private_keys)

    def _request_with_prompt(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        if self.system_prompt is None:
            return request
        updated = append_to_system_message(request.system_message, self.system_prompt)
        return request.override(system_message=updated)

    def wrap_model_call(self, request: ModelRequest[ContextT], handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]]) -> ModelResponse[ResponseT]:
        """有系统提示时追加子代理清单，然后交给下一层。"""
        return handler(self._request_with_prompt(request))

    async def awrap_model_call(self, request: ModelRequest[ContextT], handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]]) -> ModelResponse[ResponseT]:
        """异步路径与同步相同。"""
        return await handler(self._request_with_prompt(request))
