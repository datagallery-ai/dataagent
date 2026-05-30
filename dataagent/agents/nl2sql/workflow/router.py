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
from dataagent.agents.nl2sql.workflow.state import NL2SQLState
from dataagent.core.cbb.base_router import BaseRouter


class NL2SQLRouter(BaseRouter):
    def __init__(self, enabled_nodes: list[str]):
        self.enabled_nodes = enabled_nodes
        super().__init__(entry_point=enabled_nodes[0])
        self._setup_default_rules()

    def route_from_coordinator(self, state: NL2SQLState) -> str:
        return self._next("coordinator")

    def route_from_perceptor(self, state: NL2SQLState) -> str:
        return self._next("perceptor")

    def route_from_generator(self, state: NL2SQLState) -> str:
        return self._next("generator")

    def route_from_validator(self, state: NL2SQLState) -> str:
        return self._next("validator")

    def route_from_reflector(self, state: NL2SQLState) -> str:
        if state["proceed"]:
            return self._next("reflector")
        return "validator"

    def route_from_executor(self, state: NL2SQLState) -> str:
        return self._next("executor")

    def route_from_selector(self, state: NL2SQLState) -> str:
        if state["proceed"]:
            return "__end__"
        return "reflector"

    def _next(self, current: str) -> str:
        idx = self.enabled_nodes.index(current)
        if idx + 1 >= len(self.enabled_nodes):
            return "__end__"
        return self.enabled_nodes[idx + 1]

    def _setup_default_rules(self):
        self._routing_rules = {
            "coordinator": self.route_from_coordinator,
            "perceptor": self.route_from_perceptor,
            "generator": self.route_from_generator,
            "validator": self.route_from_validator,
            "reflector": self.route_from_reflector,
            "executor": self.route_from_executor,
            "selector": self.route_from_selector,
        }
