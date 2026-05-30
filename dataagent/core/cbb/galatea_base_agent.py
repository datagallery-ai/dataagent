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
"""Synchronous BaseAgent for galatea-style agents.

This is a *separate* class from ``dataagent.core.cbb.base_agent.BaseAgent`` (which
is an async ABC for FlexAgent etc.).  The two coexist until the convergence
phase merges the interfaces.

The galatea-style BaseAgent:
- Accepts ``(name, nodes, router, env)`` at construction time
- Mounts ``ActionManager`` onto the declared nodes
- Runs a synchronous ``invoke(state, env) -> state`` with pre/post hook chains
- Delegates the actual graph-loop to ``_invoke()`` in subclasses
"""

from __future__ import annotations

from dataagent.core.cbb.agent_env import Env
from dataagent.core.cbb.base_hook import BaseHook
from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.cbb.base_router import BaseRouter
from dataagent.core.cbb.base_state import BaseState
from dataagent.core.cbb.module import Module
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.managers.galatea_action_manager import ActionManager


class BaseAgent:
    def __init__(
        self,
        name: str,
        nodes: dict[str, BaseNode],
        router: BaseRouter,
        env: Env,
    ) -> None:
        self.name = name
        self._nodes = nodes
        self._router = router
        self._env = env

        self.modules: dict[str, Module] = {}
        if "action_manager" in env.modules:
            action_manager = ActionManager()
            action_manager.mount(self._env)
            self.modules["action_manager"] = action_manager

        for module_name, module in self.modules.items():
            nodes_to_mount = env.modules[module_name]
            for node_name in nodes_to_mount:
                node = self._nodes[node_name]
                node.mount_module(module_name, module)

        self._pre_hooks: list[BaseHook] = []
        self._post_hooks: list[BaseHook] = []

    def add_pre_hook(self, hook: BaseHook, side: str = "right") -> None:
        """Add a pre-hook to the agent."""
        if side == "left":
            self._pre_hooks.insert(0, hook)
            return
        if side != "right":
            raise ValueError("side must be 'left' or 'right'")
        self._pre_hooks.append(hook)

    def add_post_hook(self, hook: BaseHook, side: str = "right") -> None:
        """Add a post-hook to the agent."""
        if side == "left":
            self._post_hooks.insert(0, hook)
            return
        if side != "right":
            raise ValueError("side must be 'left' or 'right'")
        self._post_hooks.append(hook)

    def get_node(self, name: str) -> BaseNode:
        """Get a node from the agent."""
        if name not in self._nodes:
            raise ValueError(f"Node {name} not found")
        return self._nodes[name]

    def get_router(self) -> BaseRouter:
        """Get the router from the agent."""
        return self._router

    def get_modules(self) -> dict[str, Module]:
        """Get the modules from the agent."""
        return self.modules

    def invoke(self, state: BaseState, env: Env) -> BaseState:
        """Invoke the agent."""
        runtime = Runtime(env)

        for hook in self._pre_hooks:
            state = hook(state, runtime)

        state = self._invoke(state, runtime)

        for hook in self._post_hooks:
            state = hook(state, runtime)

        return state

    def _invoke(self, state: BaseState, runtime: Runtime) -> BaseState:
        """Invoke the agent."""
        raise NotImplementedError
