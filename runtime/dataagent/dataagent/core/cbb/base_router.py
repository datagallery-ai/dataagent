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
from collections.abc import Callable

from dataagent.core.cbb.base_state import BaseState


class BaseRouter:
    def __init__(self, entry_point: str):
        self._routing_rules: dict[str, Callable[[BaseState], str]] = {}
        self._entry_point = entry_point

    @property
    def routing_rules(self) -> dict[str, Callable[[BaseState], str]]:
        """Router of workflow"""
        return self._routing_rules

    @property
    def entry_point(self) -> str:
        """Entry node of workflow"""
        return self._entry_point

    # galatea-style
    @property
    def entry(self) -> str:
        """Alias for ``entry_point`` (galatea-style naming)."""
        return self._entry_point

    # galatea-style
    @property
    def rules(self) -> dict[str, Callable[[BaseState], str]]:
        """Alias for ``routing_rules`` (galatea-style naming)."""
        return self._routing_rules

    @staticmethod
    def _default_route(state: BaseState) -> str:
        # 与 langgraph 的 END 语义保持一致：结束标记为 "__end__"
        # 统一用字符串，避免 domain 层 import langgraph 常量。
        _ = state
        return "__end__"

    def route(self, state: BaseState) -> str:
        """Return next node based on current state"""
        current_node = state.current_node
        if current_node in self._routing_rules:
            return self._routing_rules[current_node](state)
        return self._default_route(state)

    def add_custom_rule(self, node_name: str, rule_func: Callable[[BaseState], str]):
        """Register route function for this node"""
        self._routing_rules[node_name] = rule_func

    def remove_custom_rule(self, node_name: str):
        """Remove route function of this node"""
        if node_name in self._routing_rules:
            del self._routing_rules[node_name]

    # galatea-style
    def add_rule(self, node_name: str, rule_func: Callable[[BaseState], str]) -> None:
        """Alias for ``add_custom_rule`` (galatea-style naming)."""
        self.add_custom_rule(node_name, rule_func)

    # galatea-style
    def process(self, curr_node: str, state: BaseState, runtime: object = None) -> str:
        """Evaluate the routing rule for ``curr_node`` and return the next node name.

        Galatea-style routers call this as ``router.process(curr_node, state, runtime)``.
        """
        if curr_node in self._routing_rules:
            return self._routing_rules[curr_node](state)
        return self._default_route(state)
