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
from dataagent.agents.bird.workflow.state import BirdState
from dataagent.core.cbb.base_router import BaseRouter


class BirdRouter(BaseRouter):
    def __init__(self):
        super().__init__(entry_point="perceptor")
        self._setup_default_rules()

    def route_from_perceptor(self, state: BirdState) -> str:
        """Route after perceptor to the next enabled node."""
        return "generator"

    def route_from_generator(self, state: BirdState) -> str:
        """Route after generator to the next enabled node."""
        if not state.get("generation_results") and state.get("budget_complete"):
            return "selector"
        return "validator"

    def route_from_validator(self, state: BirdState) -> str:
        """Route after validator to the next enabled node."""
        return "reflector"

    def route_from_reflector(self, state: BirdState) -> str:
        """Route after reflector based on proceed flag."""
        if state["proceed"]:
            return "executor"
        return "validator"

    def route_from_executor(self, state: BirdState) -> str:
        """Route after executor to the next enabled node."""
        if state.get("needs_generation_retry", False) or state.get("needs_phase2", False):
            return "generator"
        return "selector"

    def route_from_selector(self, state: BirdState) -> str:
        """Finish the workflow after the selector produces its final choice."""
        return "__end__"

    def _setup_default_rules(self):
        self._routing_rules = {
            "perceptor": self.route_from_perceptor,
            "generator": self.route_from_generator,
            "validator": self.route_from_validator,
            "reflector": self.route_from_reflector,
            "executor": self.route_from_executor,
            "selector": self.route_from_selector,
        }
