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
"""UT: L1 BaseDataAgent injects per-Agent Runtime into workflow nodes."""

from __future__ import annotations

from typing import Any

import pytest
from dataagent.core.cbb import BaseNode, BaseRouter, BaseState

from dataagent.interface.sdk import BaseDataAgent


class _L1ProbeState(BaseState):
    """Minimal state for L1 runtime probe."""

    user_query: str
    seen_db_id: str


class _L1ProbeNode(BaseNode):
    """Records DATABASE.db_id from injected runtime."""

    def __init__(self) -> None:
        super().__init__(name="probe", chat_model_name=None)

    async def _aprocess(self, state: _L1ProbeState, runtime: Any = None) -> dict[str, Any]:
        """
        Read per-Agent config via runtime and return it on state.

        Args:
            state: Workflow state.
            runtime: L1 Runtime from :meth:`BaseDataAgent._build_l1_runtime`.

        Returns:
            Partial state update with ``seen_db_id``.
        """
        if runtime is None:
            raise RuntimeError("L1 probe node requires runtime")
        db_id = runtime.get_config("DATABASE.db_id")
        return {"seen_db_id": str(db_id)}


class _L1ProbeRouter(BaseRouter):
    """Single-node router: probe -> end."""

    def __init__(self) -> None:
        super().__init__(entry_point="probe")
        self.add_custom_rule("probe", lambda _state: "__end__")


@pytest.mark.asyncio
async def test_l1_chat_passes_runtime_config_to_node() -> None:
    """BaseDataAgent.chat must bind self._config_manager on Runtime for nodes to read."""
    agent = (
        BaseDataAgent()
        .set_architecture(
            name="l1_probe",
            state_cls=_L1ProbeState,
            nodes=[_L1ProbeNode()],
            router=_L1ProbeRouter(),
        )
        .set_database(db_id="l1_ut_db", engine="sqlite", config={})
    )
    result = await agent.chat("hello")
    assert result.get("seen_db_id") == "l1_ut_db"
