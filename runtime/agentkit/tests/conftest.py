import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from dataagent.bootstrap import LaunchOptions, prepare_runtime


class RecordingCallback(AsyncCallbackHandler):
    """Observe native IDs and lifecycle methods without an application HookRuntime."""

    def __init__(self, agents=("dataagent-v2", "general-purpose")):
        self.agents = set(agents)
        self.events = []
        self.active = {}

    async def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        name = kwargs.get("name") or (serialized or {}).get("name")
        if name in self.agents:
            details = {"run_id": str(run_id), "parent_run_id": str(parent_run_id) if parent_run_id else None,
                       "thread_id": (kwargs.get("metadata") or {}).get("thread_id")}
            self.active[run_id] = (name, details)
            self.events.append((name, "on_chain_start", details))

    async def on_chain_end(self, outputs, *, run_id, **kwargs):
        if item := self.active.pop(run_id, None):
            name, details = item
            self.events.append((name, "on_chain_end", {**details, "output": outputs}))

    async def on_chain_error(self, error, *, run_id, **kwargs):
        if item := self.active.pop(run_id, None):
            name, details = item
            self.events.append((name, "on_chain_error", {**details, "error": error}))

    async def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        name = (serialized or {}).get("name")
        details = {"run_id": str(run_id), "parent_run_id": str(parent_run_id) if parent_run_id else None,
                   "thread_id": (kwargs.get("metadata") or {}).get("thread_id")}
        self.active[run_id] = (name, details)
        self.events.append((name, "on_tool_start", details))

    async def on_tool_end(self, output, *, run_id, **kwargs):
        name, details = self.active.pop(run_id)
        self.events.append((name, "on_tool_end", {**details, "output": output}))

    async def on_tool_error(self, error, *, run_id, **kwargs):
        name, details = self.active.pop(run_id)
        self.events.append((name, "on_tool_error", {**details, "error": error}))


class ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    cursor: int = 0
    requests: list = Field(default_factory=list)
    tool_schemas: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "dataagent-v2-test"

    def bind_tools(self, tools, **kwargs):
        self.tool_schemas.append([tool.name for tool in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append(messages)
        if self.cursor >= len(self.responses):
            raise RuntimeError("Scripted model ran out of responses")
        message = self.responses[self.cursor].model_copy(deep=True, update={"id": str(uuid4())})
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)


def call(name, args, id=None):
    return AIMessage(content="", tool_calls=[{
        "name": name, "args": args, "id": id or str(uuid4()), "type": "tool_call",
    }])


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DATAAGENT_WORKSPACE", str(tmp_path))
    for name in ("DATAAGENT_WORKSPACE", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def common_plugin(tmp_path):
    """Install the example as an external Home plugin, never as a builtin."""
    source = Path(__file__).resolve().parents[1] / "examples/plugins/common"
    target = tmp_path / "home/plugins/common"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return target


@pytest.fixture
def runtime(tmp_path, common_plugin):
    (tmp_path / "home/config.json").write_text(json.dumps({"plugins": {"enabled": ["common"]}}))
    path = tmp_path / "test-config.json"
    path.write_text(json.dumps({
        "models": {"primary": {
            "name": "test-model", "base_url": "https://example.invalid/v1", "api_key": "test-secret",
        }},
    }))
    return prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))


@pytest.fixture
def settings(runtime):
    return runtime.settings
