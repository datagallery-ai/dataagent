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
from __future__ import annotations

import pytest

from dataagent.core.flex.workflow.router import FlexRouter


def test_route_after_first_actor_complete_returns_post_or_end():
    router = FlexRouter(
        actor_nodes=["planner", "executor"],
        post_nodes=["post"],
    )

    assert router.routing_rules["planner"]({"complete": True}) == "post"


def test_route_after_last_actor_complete_returns_post_or_end():
    router = FlexRouter(
        actor_nodes=["planner", "executor"],
        post_nodes=["post"],
    )

    assert router.routing_rules["executor"]({"complete": True}) == "post"
