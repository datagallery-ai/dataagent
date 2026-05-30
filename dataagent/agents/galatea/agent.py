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
"""Galatea agent implementation."""

import asyncio
import inspect
import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dataagent.agents.galatea.graph.executor import ExecutorNode
from dataagent.agents.galatea.graph.planner import PlannerNode
from dataagent.agents.galatea.graph.router import GalateaRouter
from dataagent.agents.galatea.graph.rule import route_from_executor, route_from_planner
from dataagent.agents.galatea.hooks.metadata_tracker import post_metadata_tracker, pre_metadata_tracker
from dataagent.agents.galatea.hooks.portraiter import portraiter
from dataagent.agents.galatea.hooks.pruner import pruner
from dataagent.agents.galatea.hooks.streamer import streamer
from dataagent.agents.galatea.state.state import State
from dataagent.agents.galatea.utils.history_utils import load_history_messages
from dataagent.agents.galatea.utils.workspace_utils import workspace_dir_for_session
from dataagent.core.cbb.agent_env import Env
from dataagent.core.cbb.base_hook import BaseHook
from dataagent.core.cbb.galatea_base_agent import BaseAgent
from dataagent.core.cbb.runtime import Runtime


class Galatea(BaseAgent):
    def __init__(self, name: str, env: Env):
        nodes = {
            "planner": PlannerNode(),
            "executor": ExecutorNode(),
        }
        nodes["planner"].add_post_hook(streamer)
        nodes["executor"].add_pre_hook(pre_metadata_tracker)
        nodes["executor"].add_post_hook(post_metadata_tracker, side="left")
        nodes["executor"].add_post_hook(pruner)
        nodes["executor"].add_post_hook(streamer)

        router = GalateaRouter(entry="planner")
        router.add_rule("planner", route_from_planner)
        router.add_rule("executor", route_from_executor)

        if "planner" not in env.modules["action_manager"]:
            env.modules["action_manager"].append("planner")
        if "executor" not in env.modules["action_manager"]:
            env.modules["action_manager"].append("executor")

        super().__init__(name=name, nodes=nodes, router=router, env=env)
        self.add_post_hook(portraiter)

        agent_hooks = env.hooks.get("agent", {})
        for hook in agent_hooks.get("pre", []):
            self.add_pre_hook(self._validate_hook(hook, "agent.pre"))
        for hook in agent_hooks.get("post", []):
            self.add_post_hook(self._validate_hook(hook, "agent.post"))

        for node_name, node_hooks in env.hooks.get("nodes", {}).items():
            node = self.get_node(node_name)
            for hook in node_hooks.get("pre", []):
                node.add_pre_hook(self._validate_hook(hook, f"nodes.{node_name}.pre"))
            for hook in node_hooks.get("post", []):
                node.add_post_hook(self._validate_hook(hook, f"nodes.{node_name}.post"))

        action_manager = self.get_modules()["action_manager"]
        registered_skills = action_manager.get_skills()
        if registered_skills:
            action_manager.register_load_skill_tool()

    @staticmethod
    def _validate_hook(hook: object, location: str) -> BaseHook:
        if not callable(hook):
            raise TypeError(
                f"Invalid hook at {location}: expected BaseHook-compatible callable, got {type(hook).__name__}"
            )

        params = list(inspect.signature(hook).parameters.values())
        if not params or params[0].name != "state":
            raise TypeError(f"Invalid hook at {location}: first parameter must be named 'state'")
        if len(params) >= 2 and params[1].name != "runtime":
            raise TypeError(f"Invalid hook at {location}: second parameter must be named 'runtime'")
        if len(params) > 2:
            raise TypeError(f"Invalid hook at {location}: only (state) or (state, runtime) are allowed")

        return hook

    @classmethod
    @contextmanager
    def _agent_workdir(cls, workdir: Path):
        previous = Path.cwd()
        try:
            os.chdir(workdir)
            yield
        finally:
            os.chdir(previous)

    def invoke(self, state: State, env: Env) -> State:
        user_id = str(state.get("user_id", "default"))
        session_id = str(state.get("session_id", "")).strip() or str(uuid.uuid4())
        state["session_id"] = session_id
        workspace = workspace_dir_for_session(user_id, session_id)
        state["messages"] = load_history_messages(workspace)
        env.workspace_dir = workspace
        with self._agent_workdir(workspace):
            return super().invoke(state, env)

    async def chat(
        self,
        message: str,
        initial_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> State:
        """Run a single turn in a thread pool and return the final State."""
        loop = asyncio.get_running_loop()
        state = self._build_state(message, initial_state)
        return await loop.run_in_executor(None, lambda: self.invoke(state, self._env))

    async def astream(
        self,
        message: str,
        initial_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream step events while invoke runs in a thread pool.

        Yields dicts emitted by the ``streamer`` hook via ``env.event_sink``.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def event_sink(event: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        self._env.event_sink = event_sink
        state = self._build_state(message, initial_state)

        async def _run() -> None:
            try:
                await loop.run_in_executor(None, lambda: self.invoke(state, self._env))
            finally:
                await queue.put(None)  # sentinel — signals end of stream

        task = asyncio.create_task(_run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            await task

    def _invoke(self, state: State, runtime: Runtime) -> State:
        curr_node = self.get_router().entry
        while curr_node != "__end__":
            runtime.ensure_not_cancelled()
            node = self.get_node(curr_node)
            state = node.process(state, runtime)
            runtime.ensure_not_cancelled()
            curr_node = self._router.process(curr_node, state, runtime)

        return state

    # ------------------------------------------------------------------
    # Async interface (bridges sync invoke to DataAgent's async convention)
    # ------------------------------------------------------------------

    def _build_state(self, message: str, initial_state: dict[str, Any] | None = None) -> State:
        """Construct a galatea State from a plain dict / DataAgent initial_state."""
        base = initial_state or {}
        return State(
            user_query=message,
            curr_iter=0,
            messages=[],
            enable_hierarchical_orchestration=bool(base.get("enable_hierarchical_orchestration", False)),
            enable_portrait=bool(base.get("enable_portrait", False)),
            hierarchy=str(base.get("hierarchy", "MAIN")),
            user_id=str(base.get("user_id", "default")),
            session_id=str(base.get("session_id", "")),
            instructions=str(base.get("instructions", "")),
        )
