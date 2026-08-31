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
"""Unit tests for interface runtime and boundary security behavior."""

from __future__ import annotations

from typing import Any

import pytest

import dataagent.interface.sdk.base_data_agent as base_data_agent_module
from dataagent.core.cbb import BaseNode, BaseRouter, BaseState
from dataagent.interface.rest_api.service import DataAgentService
from dataagent.interface.sdk import BaseDataAgent
from dataagent.interface.sdk.builder import AgentBuilder


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
        .set_database(db_id="l1_ut_db", dialect="sqlite", config={})
    )
    result = await agent.chat("hello")
    assert result.get("seen_db_id") == "l1_ut_db"


def test_register_configs_does_not_log_config_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configuration registration logs must not contain credentials or endpoints."""
    records: list[str] = []
    monkeypatch.setattr(
        base_data_agent_module.logger,
        "debug",
        lambda message, *args: records.append(str(message).format(*args)),
    )

    database_cfg = {
        "db_id": "private_database",
        "dialect": "postgresql://admin:secret@internal-db/private",
        "config": {"password": "secret"},
    }
    metavisor_cfg = {
        "enable": True,
        "metavisor_url": "https://token@internal-metavisor",
    }
    ontology_cfg = {
        "enable": True,
        "url": "https://internal-ontology",
        "api_key": "ontology-secret",
    }
    agent = BaseDataAgent().set_database(**database_cfg)
    agent._metavisor_cfg = metavisor_cfg
    agent._ontology_cfg = ontology_cfg

    agent._register_configs()

    log_output = "\n".join(records)
    assert records
    assert "DATABASE" in log_output
    assert "secret" not in log_output
    assert "internal" not in log_output
    assert "private_database" not in log_output
    assert agent.config_manager.get("DATABASE.config.password") == database_cfg["config"]["password"]


def test_public_error_excludes_internal_detail() -> None:
    """Public error payloads must not expose internal diagnostic fields."""
    from dataagent.core.errors import DataAgentError

    error = DataAgentError(
        source="tool",
        component="sql_service",
        fact="SQL 服务调用失败；sql=select 1",
        trace_id="trace-sql",
    )
    payload = error.to_dict()
    assert payload["source"] == "tool"
    assert "code" not in payload
    assert payload["component"] == "sql_service"
    assert "sql=select 1" in payload["fact"]
    assert "detail" not in payload
    assert "locator" not in payload
    assert "secret" not in str(payload)
    assert "private_table" not in str(payload)


@pytest.mark.parametrize(
    "state",
    [
        {
            "success": False,
            "message": "Agent failed",
            "workspace": "/srv/private",
            "database": {"host": "internal-db", "password": "secret"},
        },
        {"error": RuntimeError("password=secret path=/srv/private/config.yaml")},
        "password=secret path=/srv/private/config.yaml",
    ],
)
def test_format_result_raises_without_exposing_internal_state(state: Any) -> None:
    """Invalid or failed workflow state must raise a public DataAgentError."""
    service = DataAgentService()
    with pytest.raises(Exception) as caught:
        service._format_result(state)
    public = caught.value.to_dict() if hasattr(caught.value, "to_dict") else {"message": str(caught.value)}
    assert "secret" not in str(public)
    assert "/srv/private" not in str(public)


@pytest.mark.parametrize("agent_type", ["../../../escaped", "chatbi"])
def test_load_from_dict_rejects_unsupported_agent_type(agent_type: str) -> None:
    """External agent_type values must not be usable as path fragments."""
    builder = AgentBuilder()

    with pytest.raises(ValueError, match=r"Unsupported `agent_type`"):
        builder.from_config(
            config={
                "AGENT_CONFIG": {
                    "agent_type": agent_type,
                    "name": "audit",
                }
            }
        )
