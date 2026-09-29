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
"""把任务交给远程 Agent Protocol 上的后台子代理。

五个工具负责启动、查看、追加说明、取消和列举。启动后立刻返回 task_id，
不在这里等远程跑完。任务表写在状态的 `async_tasks` 通道里。
"""

import asyncio as _asyncio
import json as _json
import logging as _logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NotRequired, TypedDict

import langchain.agents.middleware.types as lc_mw
import langgraph_sdk
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command, TracePolicy, omit_payload
from langgraph_sdk.client import LangGraphClient, SyncLangGraphClient
from langgraph_sdk.schema import Run
from pydantic import BaseModel, Field

from dataagent.core.middleware._utils import append_to_system_message

logger = _logging.getLogger(__name__)

_Str = str

ASYNC_TASK_TOOL_DESCRIPTION = (
    "Start an async subagent on a remote server. The subagent runs in the background "
    "and returns a task ID immediately.\n"
    "\n"
    "Available async agent types:\n"
    "{available_agents}\n"
    "\n"
    "## Usage notes:\n"
    "1. This tool launches a background task and returns immediately with a task ID. "
    "Report the task ID to the user and stop — do NOT immediately check status.\n"
    "2. Use `check_async_task` only when the user asks for a status update or result.\n"
    "3. Use `update_async_task` to send new instructions to a running task.\n"
    "4. Multiple async subagents can run concurrently — launch several and let them run in the background.\n"
    "5. The subagent runs on a remote server, so it has its own tools and capabilities."
)
_CHECK_TOOL_DESCRIPTION = (
    "Check the status of an async subagent task. Returns the current status and, if complete, the result. "
    "Statuses shown earlier in the conversation are always stale, so call this to get the current status "
    "rather than reporting a status from a previous tool result."
)
_UPDATE_TOOL_DESCRIPTION = (
    "Send updated instructions to an async subagent. Interrupts the current run and starts "
    "a new one on the same thread, so the subagent sees the full conversation history plus "
    "your new message. The task_id remains the same."
)
_CANCEL_TOOL_DESCRIPTION = "Cancel a running async subagent task. Use this to stop a task that is no longer needed."
_LIST_TOOL_DESCRIPTION = (
    "List tracked async subagent tasks with their current live statuses. "
    "By default shows all tasks. Use `status_filter` to narrow by status "
    "(e.g. 'running', 'success', 'error', 'cancelled'). "
    "Use `check_async_task` to get the full result of a specific completed task. "
    "Statuses shown earlier in the conversation are always stale, so call this to read current "
    "statuses rather than reporting one from a previous tool result."
)
_FIELD_TASK = "A detailed description of the task for the async subagent to perform."
_FIELD_AGENT_TYPE = "The type of async subagent to use. Must be one of the available types listed in the tool description."
_FIELD_TASK_ID = "The exact task_id string returned by start_async_task. Pass it verbatim."
_FIELD_MESSAGE = "Follow-up instructions or context to send to the subagent."
_FIELD_STATUS_FILTER = "Filter tasks by status. One of: 'running', 'success', 'error', 'cancelled', 'all'. Defaults to 'all'."
_NEED_ONE_AGENT = "At least one async subagent must be specified"
_NO_OUTPUT = "(completed with no output messages)"
_ERROR_FALLBACK = "The async subagent encountered an error."
_NO_TRACKED = "No async subagent tasks tracked."
_LAUNCH_LOG = "Failed to launch async subagent '%s': %s"
_UPDATE_LOG = "Failed to update async subagent '%s': %s"
_THREAD_LOG = "Failed to fetch thread values for task %s: %s"
_LIVE_LOG = "Failed to fetch live status for task %s (agent=%s), returning cached status %r"
_AUTH_HEADER = "x-auth-scheme"
_AUTH_SCHEME = "langsmith"
_DONE = frozenset(("cancelled", "success", "error", "timeout", "interrupted"))
_INTERRUPT = "interrupt"
_STATUS_FILTER = Literal["running", "success", "error", "cancelled", "all"]


class AsyncSubAgent(TypedDict):
    """远程服务器上的一种后台子代理。"""

    name: _Str
    description: _Str
    graph_id: _Str
    url: NotRequired[_Str]
    headers: NotRequired[dict[_Str, _Str]]


class AsyncTask(TypedDict):
    """记在状态里的一条后台任务。"""

    task_id: _Str
    agent_name: _Str
    thread_id: _Str
    run_id: _Str
    status: _Str
    created_at: _Str
    last_checked_at: _Str
    last_updated_at: _Str


def _merge_tasks(existing: dict[str, AsyncTask] | None, update: dict[str, AsyncTask]) -> dict[str, AsyncTask]:
    """浅合并任务表。假的旧表当成空表，同名键整个换掉。"""
    merged = dict(existing or {})
    merged.update(update)
    return merged


class AsyncSubAgentState(lc_mw.AgentState):
    """为主代理加上 `async_tasks` 通道。"""

    async_tasks: Annotated[NotRequired[dict[str, AsyncTask]], _merge_tasks]


class StartAsyncTaskSchema(BaseModel):
    """Input schema for the `start_async_task` tool."""

    description: str = Field(description=_FIELD_TASK)
    subagent_type: str = Field(description=_FIELD_AGENT_TYPE)


class CheckAsyncTaskSchema(BaseModel):
    """Input schema for the `check_async_task` tool."""

    task_id: str = Field(description=_FIELD_TASK_ID)


class UpdateAsyncTaskSchema(BaseModel):
    """Input schema for the `update_async_task` tool."""

    task_id: str = Field(description=_FIELD_TASK_ID)
    message: str = Field(description=_FIELD_MESSAGE)


class CancelAsyncTaskSchema(BaseModel):
    """Input schema for the `cancel_async_task` tool."""

    task_id: str = Field(description=_FIELD_TASK_ID)


class ListAsyncTasksSchema(BaseModel):
    """Input schema for the `list_async_tasks` tool."""

    status_filter: _STATUS_FILTER | None = Field(default=None, description=_FIELD_STATUS_FILTER)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _headers(spec: AsyncSubAgent) -> dict[str, str]:
    copied = dict(spec.get("headers") or {})
    if _AUTH_HEADER not in copied:
        copied[_AUTH_HEADER] = _AUTH_SCHEME
    return copied


def _catalog(agents: list[AsyncSubAgent]) -> str:
    return "\n".join(f"- {item['name']}: {item['description']}" for item in agents)


def _user_turn(text: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}]}


def _record(
    identifier: str,
    agent: str,
    thread: str,
    run: str,
    status: str,
    created: str,
    checked: str,
    updated: str,
) -> AsyncTask:
    return {
        "task_id": identifier,
        "agent_name": agent,
        "thread_id": thread,
        "run_id": run,
        "status": status,
        "created_at": created,
        "last_checked_at": checked,
        "last_updated_at": updated,
    }


def _reply(text: str, call_id: str | None, tasks: dict[str, AsyncTask]) -> Command:
    note = ToolMessage(text, tool_call_id=call_id)
    return Command(update={"messages": [note], "async_tasks": tasks})


class _RemoteClients:
    """按 (url, 解析后的头) 复用同步和异步客户端。"""

    def __init__(self, specs: dict[str, AsyncSubAgent]) -> None:
        self._specs = specs
        self._sync_pool: dict[tuple[Any, frozenset[tuple[str, str]]], SyncLangGraphClient] = {}
        self._async_pool: dict[tuple[Any, frozenset[tuple[str, str]]], LangGraphClient] = {}

    def _key(self, spec: AsyncSubAgent) -> tuple[Any, frozenset[tuple[str, str]]]:
        return (spec.get("url"), frozenset(_headers(spec).items()))

    def sync_for(self, agent_name: str) -> SyncLangGraphClient:
        spec = self._specs[agent_name]
        if spec.get("url") is None:
            raise ValueError(
                f"Async subagent '{agent_name}' has no url configured. ASGI transport (url=None) requires async invocation."
            )
        key = self._key(spec)
        if key not in self._sync_pool:
            self._sync_pool[key] = langgraph_sdk.get_sync_client(url=spec.get("url"), headers=_headers(spec))
        return self._sync_pool[key]

    def async_for(self, agent_name: str) -> LangGraphClient:
        spec = self._specs[agent_name]
        key = self._key(spec)
        if key not in self._async_pool:
            self._async_pool[key] = langgraph_sdk.get_client(url=spec.get("url"), headers=_headers(spec))
        return self._async_pool[key]


def _unknown_type(known: dict[str, AsyncSubAgent], requested: str) -> str | None:
    if requested in known:
        return None
    listed = ", ".join(f"`{key}`" for key in known)
    return f"Unknown async subagent type `{requested}`. Available types: {listed}"


def _tracked(raw_id: str, runtime: ToolRuntime) -> AsyncTask | str:
    table = runtime.state.get("async_tasks") or {}
    found = table.get(raw_id.strip())
    if not found:
        return f"No tracked task found for task_id: {raw_id!r}"
    return found


def _status_payload(run: Run, thread_id: str, thread_values: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": run["status"], "thread_id": thread_id}
    kind = run["status"]
    if kind == "success":
        messages = thread_values.get("messages", []) if isinstance(thread_values, dict) else []
        if not messages:
            payload["result"] = _NO_OUTPUT
        else:
            last = messages[-1]
            payload["result"] = last.get("content", "") if isinstance(last, dict) else str(last)
    elif kind == "error":
        detail = run.get("error")
        payload["error"] = str(detail) if detail else _ERROR_FALLBACK
    return payload


def _checked(payload: dict[str, Any], task: AsyncTask, call_id: str | None) -> Command:
    stamp = _stamp()
    updated_at = stamp if task["status"] != payload["status"] else task["last_updated_at"]
    refreshed = _record(
        task["task_id"],
        task["agent_name"],
        task["thread_id"],
        task["run_id"],
        payload["status"],
        task["created_at"],
        stamp,
        updated_at,
    )
    return _reply(_json.dumps(payload), call_id, {task["task_id"]: refreshed})


def _row(task: AsyncTask, status: str) -> str:
    return f"- task_id: {task['task_id']}  agent: {task['agent_name']}  status: {status}"


def _with_live(task: AsyncTask, status: str, stamp: str) -> AsyncTask:
    updated_at = stamp if status != task["status"] else task["last_updated_at"]
    return _record(
        task["task_id"],
        task["agent_name"],
        task["thread_id"],
        task["run_id"],
        status,
        task["created_at"],
        stamp,
        updated_at,
    )


def _pick(tasks: dict[str, AsyncTask], wanted: str | None) -> list[AsyncTask]:
    if not wanted or wanted == "all":
        return list(tasks.values())
    return [item for item in tasks.values() if item["status"] == wanted]


def _remember_thread(client: Any, task: AsyncTask) -> Any:
    try:
        thread = client.threads.get(thread_id=task["thread_id"])
        return thread.get("values") or {}
    except Exception as exc:
        logger.warning(_THREAD_LOG, task["task_id"], exc)
        return {}


async def _remember_thread_async(client: Any, task: AsyncTask) -> Any:
    try:
        thread = await client.threads.get(thread_id=task["thread_id"])
        return thread.get("values") or {}
    except Exception as exc:
        logger.warning(_THREAD_LOG, task["task_id"], exc)
        return {}


def _live_sync(pool: _RemoteClients, task: AsyncTask) -> str:
    if task["status"] in _DONE:
        return task["status"]
    try:
        client = pool.sync_for(task["agent_name"])
        run = client.runs.get(thread_id=task["thread_id"], run_id=task["run_id"])
        return run["status"]
    except Exception:
        logger.warning(_LIVE_LOG, task["task_id"], task["agent_name"], task["status"], exc_info=True)
        return task["status"]


async def _live_async(pool: _RemoteClients, task: AsyncTask) -> str:
    if task["status"] in _DONE:
        return task["status"]
    try:
        client = pool.async_for(task["agent_name"])
        run = await client.runs.get(thread_id=task["thread_id"], run_id=task["run_id"])
        return run["status"]
    except Exception:
        logger.warning(_LIVE_LOG, task["task_id"], task["agent_name"], task["status"], exc_info=True)
        return task["status"]


def _bind_tool(function, coroutine, name: str, schema: type[BaseModel], description: str) -> StructuredTool:
    return StructuredTool.from_function(
        func=function,
        coroutine=coroutine,
        name=name,
        args_schema=schema,
        description=description,
        infer_schema=False,
    )


def _make_start(known: dict[str, AsyncSubAgent], pool: _RemoteClients, tool_description: str) -> StructuredTool:
    def start_async_task(description: str, subagent_type: str, runtime: ToolRuntime) -> str | Command:
        problem = _unknown_type(known, subagent_type)
        if problem:
            return problem
        spec = known[subagent_type]
        try:
            client = pool.sync_for(subagent_type)
            thread = client.threads.create()
            run = client.runs.create(
                assistant_id=spec["graph_id"],
                input=_user_turn(description),
                thread_id=thread["thread_id"],
            )
        except Exception as exc:
            logger.warning(_LAUNCH_LOG, subagent_type, exc)
            return f"Failed to launch async subagent '{subagent_type}': {exc}"
        stamp = _stamp()
        task_id = thread["thread_id"]
        task = _record(task_id, subagent_type, task_id, run["run_id"], "running", stamp, stamp, stamp)
        return _reply(f"Launched async subagent. task_id: {task_id}", runtime.tool_call_id, {task_id: task})

    async def astart_async_task(description: str, subagent_type: str, runtime: ToolRuntime) -> str | Command:
        problem = _unknown_type(known, subagent_type)
        if problem:
            return problem
        spec = known[subagent_type]
        try:
            client = pool.async_for(subagent_type)
            thread = await client.threads.create()
            run = await client.runs.create(
                assistant_id=spec["graph_id"],
                input=_user_turn(description),
                thread_id=thread["thread_id"],
            )
        except Exception as exc:
            logger.warning(_LAUNCH_LOG, subagent_type, exc)
            return f"Failed to launch async subagent '{subagent_type}': {exc}"
        stamp = _stamp()
        task_id = thread["thread_id"]
        task = _record(task_id, subagent_type, task_id, run["run_id"], "running", stamp, stamp, stamp)
        return _reply(f"Launched async subagent. task_id: {task_id}", runtime.tool_call_id, {task_id: task})

    return _bind_tool(start_async_task, astart_async_task, "start_async_task", StartAsyncTaskSchema, tool_description)


def _make_check(pool: _RemoteClients) -> StructuredTool:
    def check_async_task(task_id: str, runtime: ToolRuntime) -> str | Command:
        task = _tracked(task_id, runtime)
        if isinstance(task, str):
            return task
        try:
            client = pool.sync_for(task["agent_name"])
            run = client.runs.get(thread_id=task["thread_id"], run_id=task["run_id"])
        except Exception as exc:
            return f"Failed to get run status: {exc}"
        values: Any = {}
        if run["status"] == "success":
            values = _remember_thread(client, task)
        return _checked(_status_payload(run, task["thread_id"], values), task, runtime.tool_call_id)

    async def acheck_async_task(task_id: str, runtime: ToolRuntime) -> str | Command:
        task = _tracked(task_id, runtime)
        if isinstance(task, str):
            return task
        client = pool.async_for(task["agent_name"])
        try:
            run = await client.runs.get(thread_id=task["thread_id"], run_id=task["run_id"])
        except Exception as exc:
            return f"Failed to get run status: {exc}"
        values: Any = {}
        if run["status"] == "success":
            values = await _remember_thread_async(client, task)
        return _checked(_status_payload(run, task["thread_id"], values), task, runtime.tool_call_id)

    return _bind_tool(check_async_task, acheck_async_task, "check_async_task", CheckAsyncTaskSchema, _CHECK_TOOL_DESCRIPTION)


def _make_update(known: dict[str, AsyncSubAgent], pool: _RemoteClients) -> StructuredTool:
    def update_async_task(task_id: str, message: str, runtime: ToolRuntime) -> str | Command:
        tracked = _tracked(task_id, runtime)
        if isinstance(tracked, str):
            return tracked
        spec = known[tracked["agent_name"]]
        try:
            client = pool.sync_for(tracked["agent_name"])
            run = client.runs.create(
                thread_id=tracked["thread_id"],
                assistant_id=spec["graph_id"],
                input=_user_turn(message),
                multitask_strategy=_INTERRUPT,
            )
        except Exception as exc:
            logger.warning(_UPDATE_LOG, tracked["agent_name"], exc)
            return f"Failed to update async subagent: {exc}"
        stamp = _stamp()
        task = _record(
            tracked["task_id"],
            tracked["agent_name"],
            tracked["thread_id"],
            run["run_id"],
            "running",
            tracked["created_at"],
            tracked["last_checked_at"],
            stamp,
        )
        text = f"Updated async subagent. task_id: {tracked['task_id']}"
        return _reply(text, runtime.tool_call_id, {tracked["task_id"]: task})

    async def aupdate_async_task(task_id: str, message: str, runtime: ToolRuntime) -> str | Command:
        tracked = _tracked(task_id, runtime)
        if isinstance(tracked, str):
            return tracked
        spec = known[tracked["agent_name"]]
        try:
            client = pool.async_for(tracked["agent_name"])
            run = await client.runs.create(
                thread_id=tracked["thread_id"],
                assistant_id=spec["graph_id"],
                input=_user_turn(message),
                multitask_strategy=_INTERRUPT,
            )
        except Exception as exc:
            logger.warning(_UPDATE_LOG, tracked["agent_name"], exc)
            return f"Failed to update async subagent: {exc}"
        stamp = _stamp()
        task = _record(
            tracked["task_id"],
            tracked["agent_name"],
            tracked["thread_id"],
            run["run_id"],
            "running",
            tracked["created_at"],
            tracked["last_checked_at"],
            stamp,
        )
        text = f"Updated async subagent. task_id: {tracked['task_id']}"
        return _reply(text, runtime.tool_call_id, {tracked["task_id"]: task})

    return _bind_tool(update_async_task, aupdate_async_task, "update_async_task", UpdateAsyncTaskSchema, _UPDATE_TOOL_DESCRIPTION)


def _make_cancel(pool: _RemoteClients) -> StructuredTool:
    def cancel_async_task(task_id: str, runtime: ToolRuntime) -> str | Command:
        tracked = _tracked(task_id, runtime)
        if isinstance(tracked, str):
            return tracked
        try:
            client = pool.sync_for(tracked["agent_name"])
            client.runs.cancel(thread_id=tracked["thread_id"], run_id=tracked["run_id"])
        except Exception as exc:
            return f"Failed to cancel run: {exc}"
        stamp = _stamp()
        updated = _record(
            tracked["task_id"],
            tracked["agent_name"],
            tracked["thread_id"],
            tracked["run_id"],
            "cancelled",
            tracked["created_at"],
            stamp,
            stamp,
        )
        text = f"Cancelled async subagent task: {tracked['task_id']}"
        return _reply(text, runtime.tool_call_id, {tracked["task_id"]: updated})

    async def acancel_async_task(task_id: str, runtime: ToolRuntime) -> str | Command:
        tracked = _tracked(task_id, runtime)
        if isinstance(tracked, str):
            return tracked
        client = pool.async_for(tracked["agent_name"])
        try:
            await client.runs.cancel(thread_id=tracked["thread_id"], run_id=tracked["run_id"])
        except Exception as exc:
            return f"Failed to cancel run: {exc}"
        stamp = _stamp()
        updated = _record(
            tracked["task_id"],
            tracked["agent_name"],
            tracked["thread_id"],
            tracked["run_id"],
            "cancelled",
            tracked["created_at"],
            stamp,
            stamp,
        )
        text = f"Cancelled async subagent task: {tracked['task_id']}"
        return _reply(text, runtime.tool_call_id, {tracked["task_id"]: updated})

    return _bind_tool(cancel_async_task, acancel_async_task, "cancel_async_task", CancelAsyncTaskSchema, _CANCEL_TOOL_DESCRIPTION)


def _make_list(pool: _RemoteClients) -> StructuredTool:
    def list_async_tasks(
        runtime: ToolRuntime,
        status_filter: _STATUS_FILTER | None = None,
    ) -> str | Command:
        chosen = _pick(runtime.state.get("async_tasks") or {}, status_filter)
        if not chosen:
            return _NO_TRACKED
        stamp = _stamp()
        lines: list[str] = []
        updates: dict[str, AsyncTask] = {}
        for task in chosen:
            status = _live_sync(pool, task)
            lines.append(_row(task, status))
            updates[task["task_id"]] = _with_live(task, status, stamp)
        text = f"{len(lines)} tracked task(s):\n" + "\n".join(lines)
        return _reply(text, runtime.tool_call_id, updates)

    async def alist_async_tasks(
        runtime: ToolRuntime,
        status_filter: _STATUS_FILTER | None = None,
    ) -> str | Command:
        chosen = _pick(runtime.state.get("async_tasks") or {}, status_filter)
        if not chosen:
            return _NO_TRACKED
        statuses = await _asyncio.gather(*(_live_async(pool, task) for task in chosen))
        stamp = _stamp()
        lines: list[str] = []
        updates: dict[str, AsyncTask] = {}
        for task, status in zip(chosen, statuses, strict=True):
            lines.append(_row(task, status))
            updates[task["task_id"]] = _with_live(task, status, stamp)
        text = f"{len(lines)} tracked task(s):\n" + "\n".join(lines)
        return _reply(text, runtime.tool_call_id, updates)

    return _bind_tool(list_async_tasks, alist_async_tasks, "list_async_tasks", ListAsyncTasksSchema, _LIST_TOOL_DESCRIPTION)


def _assemble(agents: list[AsyncSubAgent]) -> list[StructuredTool]:
    known = {item["name"]: item for item in agents}
    pool = _RemoteClients(known)
    launch = ASYNC_TASK_TOOL_DESCRIPTION.format(available_agents=_catalog(agents))
    return [
        _make_start(known, pool, launch),
        _make_check(pool),
        _make_update(known, pool),
        _make_cancel(pool),
        _make_list(pool),
    ]


class AsyncSubAgentMiddleware(lc_mw.AgentMiddleware[Any, lc_mw.ContextT, lc_mw.ResponseT]):
    """给主代理挂上五个远程后台任务工具。"""

    trace_policy = TracePolicy(process_inputs=omit_payload)
    state_schema = AsyncSubAgentState

    def __init__(self, *, async_subagents: list[AsyncSubAgent], system_prompt: str | None = None) -> None:
        super().__init__()
        if not async_subagents:
            raise ValueError(_NEED_ONE_AGENT)
        names = [item["name"] for item in async_subagents]
        dupes = {name for name in names if names.count(name) > 1}
        if dupes:
            raise ValueError(f"Duplicate async subagent names: {dupes}")
        self.tools = _assemble(async_subagents)
        if system_prompt:
            self.system_prompt: str | None = system_prompt + "\n\nAvailable async subagent types:\n\n" + _catalog(async_subagents)
        else:
            self.system_prompt = system_prompt

    def wrap_model_call(
        self,
        request: lc_mw.ModelRequest[lc_mw.ContextT],
        handler: Callable[[lc_mw.ModelRequest[lc_mw.ContextT]], lc_mw.ModelResponse[lc_mw.ResponseT]],
    ) -> lc_mw.ModelResponse[lc_mw.ResponseT]:
        if self.system_prompt is not None:
            updated = append_to_system_message(request.system_message, self.system_prompt)
            return handler(request.override(system_message=updated))
        return handler(request)

    async def awrap_model_call(
        self,
        request: lc_mw.ModelRequest[lc_mw.ContextT],
        handler: Callable[[lc_mw.ModelRequest[lc_mw.ContextT]], Awaitable[lc_mw.ModelResponse[lc_mw.ResponseT]]],
    ) -> lc_mw.ModelResponse[lc_mw.ResponseT]:
        if self.system_prompt is not None:
            updated = append_to_system_message(request.system_message, self.system_prompt)
            return await handler(request.override(system_message=updated))
        return await handler(request)
