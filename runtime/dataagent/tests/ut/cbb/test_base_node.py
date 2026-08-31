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

from dataagent.core.cbb.base_node import BaseNode


class FakeNode(BaseNode):
    def __init__(self, name: str = "fake", output: dict | None = None):
        super().__init__(name=name)
        self._output = output or {"messages": []}

    async def _aprocess(self, state, runtime=None):
        state = dict(state)
        state.setdefault("node_called", []).append(self.name)
        state.update(self._output)
        return state


@pytest.mark.asyncio()
async def test_pre_hook_sets_complete_short_circuits_node():
    calls = []

    def pre_hook(state, runtime=None):
        calls.append("pre")
        new_state = dict(state)
        new_state["complete"] = True
        new_state["hook_message"] = "missing info"
        return new_state

    node = FakeNode()
    node.add_pre_hook(pre_hook)

    state = {"messages": [], "complete": False}
    result = await node.aprocess(state)

    assert calls == ["pre"]
    assert result["complete"] is True
    assert result["hook_message"] == "missing info"
    assert "node_called" not in result


@pytest.mark.asyncio()
async def test_post_hooks_run_when_not_complete():
    post_calls = []

    def post_hook(state, runtime=None):
        post_calls.append("post")
        new_state = dict(state)
        new_state["post_ran"] = True
        return new_state

    node = FakeNode(output={"messages": ["msg"]})
    node.add_post_hook(post_hook)

    result = await node.aprocess({"messages": []})

    assert post_calls == ["post"]
    assert result["post_ran"] is True
    assert result["messages"] == ["msg"]
