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
from dataagent.core.cbb.agent_env import Env
from dataagent.core.cbb.runtime import Runtime

from dataagent.config.config_manager import ConfigManager


def _minimal_env(**kwargs) -> Env:
    """Build a minimal Env for unit tests."""
    return Env(
        llm_configs={},
        tavily_configs={},
        modules={},
        hooks={},
        **kwargs,
    )


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
