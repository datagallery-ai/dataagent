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
"""Runtime per-Agent ConfigManager access tests."""

import pytest

from dataagent.config.config_manager import ConfigManager
from dataagent.core.cbb.agent_env import Env
from dataagent.core.cbb.runtime import Runtime


def _minimal_env(**kwargs) -> Env:
    """Build a minimal Env for unit tests."""
    defaults = {
        "llm_configs": {},
        "tavily_configs": {},
        "modules": {},
        "hooks": {},
    }
    defaults.update(kwargs)
    return Env(**defaults)


class TestRuntimeConfig:
    """Verify Runtime exposes per-Agent ConfigManager without global fallback."""

    def test_runtime_config_manager_and_get_config(self):
        """runtime.config_manager and get_config read from bound AgentEnv."""
        agent_cm = ConfigManager()
        agent_cm.set("DATABASE.db_id", "RUNTIME_DB")
        env = _minimal_env(config_manager=agent_cm)
        runtime = Runtime(env)

        assert runtime.config_manager is agent_cm
        assert runtime.get_config("DATABASE.db_id") == "RUNTIME_DB"

    def test_get_all_config_returns_deep_copy(self):
        """External mutation of get_all_config() result must not affect stored settings."""
        agent_cm = ConfigManager()
        agent_cm.set("DATABASE.db_id", "RUNTIME_DB")
        env = _minimal_env(config_manager=agent_cm)
        runtime = Runtime(env)

        snapshot = runtime.get_all_config()
        snapshot["DATABASE"]["db_id"] = "MUTATED"
        assert runtime.get_config("DATABASE.db_id") == "RUNTIME_DB"

    def test_missing_config_manager_raises(self):
        """Runtime without config_manager must fail fast, not fall back to global."""
        runtime = Runtime(_minimal_env())
        with pytest.raises(RuntimeError, match="config_manager"):
            runtime.get_config("DATABASE.db_id")

    def test_llm_missing_api_base_reports_field_without_leaking_api_key(self):
        """Incomplete LLM config errors identify bad fields without raw credential values."""
        runtime = Runtime(_minimal_env(llm_configs={"planner": {"model": "test-model", "api_key": "secret-key"}}))

        with pytest.raises(RuntimeError) as excinfo:
            runtime.llm("planner")

        message = str(excinfo.value)
        assert "env.llm_configs['planner'].api_base" in message
        assert "env.llm_configs['planner'].api_key" not in message
        assert "secret-key" not in message

    def test_llm_missing_api_key_reports_field_without_leaking_api_base(self):
        """Missing api_key should name the field without showing other configured values."""
        runtime = Runtime(
            _minimal_env(llm_configs={"planner": {"model": "test-model", "api_base": "https://secret-host/v1"}})
        )

        with pytest.raises(RuntimeError) as excinfo:
            runtime.llm("planner")

        message = str(excinfo.value)
        assert "env.llm_configs['planner'].api_key" in message
        assert "https://secret-host/v1" not in message
