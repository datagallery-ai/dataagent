"""Call a remote A2A 1.0 agent and expose it as one LangChain tool.

The client speaks the same JSON-RPC binding as ``dataagent serve-a2a``.
It discovers the agent card, sends ``SendMessage``, and keeps the returned
``contextId`` so the next call continues that conversation.
"""

import asyncio
import re
from dataclasses import dataclass

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import get_message_text, new_text_message
from a2a.types.a2a_pb2 import Message, Role, SendMessageRequest, Task, TaskState
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool

from dataagent.declarations import NAME_PATTERN
from dataagent.errors import DataAgentError

# Matches dataagent.limits.timeout_seconds. A remote DataAgent turn uses the same budget.
DEFAULT_TIMEOUT = 180.0
_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"


@dataclass(frozen=True)
class A2AResult:
    """One remote task, or a direct message when the peer does not open a task."""

    text: str
    context_id: str
    task_id: str
    state: str


class A2ACallError(DataAgentError, RuntimeError):
    """The remote agent returned a failed or canceled task."""

    error_code = "TOOL_ERROR"

    def __init__(self, message: str):
        super().__init__(source="tool", component="a2a", fact=message)


class A2ACaller:
    """One remote agent. Each local thread keeps its own remote context id."""

    def __init__(
        self,
        base_url: str,
        *,
        auth_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        context_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        if not base_url or not base_url.strip():
            raise ValueError("A2A base_url must not be blank")
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout
        self._contexts: dict[str | None, str] = {}
        self._tasks: dict[str | None, str] = {}
        if context_id:
            self._contexts[None] = context_id
        self._http = http_client
        self._lock = asyncio.Lock()

    @property
    def context_id(self) -> str | None:
        """Context used by calls that do not name a local thread."""
        return self._contexts.get(None)

    async def send(self, text: str, *, thread_id: str | None = None) -> A2AResult:
        """Send one user message and return the remote agent's text.

        ``thread_id`` is the local LangGraph thread. Calls that share it continue
        one remote conversation. A different local thread starts its own. A task
        left in ``TASK_STATE_INPUT_REQUIRED`` is continued by its task id.
        """
        message = text.strip()
        if not message:
            raise ValueError("A2A message must not be blank")
        async with self._lock:
            result = await self._send(
                message, self._contexts.get(thread_id), self._tasks.get(thread_id)
            )
            if result.context_id:
                self._contexts[thread_id] = result.context_id
            if result.state == _INPUT_REQUIRED and result.task_id:
                self._tasks[thread_id] = result.task_id
            else:
                self._tasks.pop(thread_id, None)
            return result

    async def _send(self, text: str, context_id: str | None, task_id: str | None) -> A2AResult:
        headers = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        owns_client = self._http is None
        http = self._http or httpx.AsyncClient(timeout=self.timeout, headers=headers)
        try:
            client = await create_client(
                agent=self.base_url,
                client_config=ClientConfig(
                    httpx_client=http, streaming=False, supported_protocol_bindings=["JSONRPC"]
                ),
            )
            request = SendMessageRequest(
                message=new_text_message(
                    text=text,
                    role=Role.ROLE_USER,
                    context_id=context_id,
                    task_id=task_id,
                )
            )
            task = None
            message = None
            async for response in client.send_message(request):
                if response.HasField("task"):
                    task = response.task
                elif response.HasField("message"):
                    message = response.message
            if task is not None:
                return _result(task)
            if message is not None:
                return _message_result(message)
            raise A2ACallError("Remote agent returned no task")
        finally:
            if owns_client:
                await http.aclose()


def remote_agent_tool(
    agent_id: str,
    base_url: str,
    *,
    auth_token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    description: str = "",
    http_client: httpx.AsyncClient | None = None,
) -> BaseTool:
    """Build a tool that forwards ``message`` to one remote A2A agent.

    The tool reads the local ``thread_id`` from the runnable config. Turns in
    one local thread share a remote context. Another local thread does not.
    """
    if re.fullmatch(NAME_PATTERN, agent_id) is None:
        raise ValueError(f"A2A agent id must match {NAME_PATTERN}")
    caller = A2ACaller(
        base_url, auth_token=auth_token, timeout=timeout, http_client=http_client
    )

    async def send(message: str, config: RunnableConfig) -> str:
        configurable = config.get("configurable") if isinstance(config, dict) else None
        thread_id = configurable.get("thread_id") if isinstance(configurable, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            thread_id = None
        result = await caller.send(message, thread_id=thread_id)
        if result.state == _INPUT_REQUIRED:
            return f"INPUT_REQUIRED\n{result.text}"
        return result.text

    return StructuredTool.from_function(
        coroutine=send,
        name=f"a2a__{agent_id}",
        description=description or f"Send a text message to the A2A agent {agent_id}.",
    )


def load_a2a_tools(runtime) -> list[BaseTool]:
    """Build one tool per configured remote agent. No network call yet."""
    tools = []
    for spec in runtime.settings.a2a.remote_agents:
        token = None if spec.auth_token is None else spec.auth_token.get_secret_value()
        timeout = spec.timeout_seconds
        if timeout is None:
            timeout = runtime.timeout_seconds
        tools.append(remote_agent_tool(
            spec.id,
            str(spec.url).rstrip("/"),
            auth_token=token,
            timeout=timeout,
            description=spec.description,
        ))
    return tools


def _result(task: Task) -> A2AResult:
    state = TaskState.Name(task.status.state)
    text = _task_text(task)
    if state in {"TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}:
        raise A2ACallError(text or state)
    return A2AResult(
        text=text, context_id=task.context_id, task_id=task.id, state=state
    )


def _message_result(message: Message) -> A2AResult:
    return A2AResult(
        text=get_message_text(message),
        context_id=message.context_id,
        task_id=message.task_id,
        state="MESSAGE",
    )


def _task_text(task: Task) -> str:
    parts = [part.text for artifact in task.artifacts for part in artifact.parts if part.text]
    if parts:
        return "".join(parts)
    if task.status.HasField("message"):
        return "".join(part.text for part in task.status.message.parts if part.text)
    return ""
