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
"""Unit tests for env_config module."""

from dataagent.actions.environment.env_config import from_config
from dataagent.config.config_manager import ConfigManager


class TestEnvConfigConfigManagerInjection:
    """Tests for per-Agent config_manager injection when creating Env instances."""

    def test_sqlite_env_receives_config_manager(self, tmp_path):
        """SQLiteEnv reads DATABASE.config.path from injected ConfigManager."""
        db_path = tmp_path / "test.sqlite"
        db_path.write_text("")

        config_manager = ConfigManager()
        config_manager.settings = {
            "DATABASE": {
                "config": {
                    "path": str(db_path),
                }
            }
        }

        env = from_config(
            {"module": "dataagent.actions.gym.SQLiteEnv"},
            config_manager=config_manager,
        )

        assert env.db_path == str(db_path)

    def test_arithmetic_env_ignores_config_manager(self):
        """Env classes without config_manager param are unaffected by injection."""
        env = from_config(
            {"module": "dataagent.actions.gym.ArithmeticEnv"},
            config_manager=ConfigManager(),
        )

        assert env.__class__.__name__ == "ArithmeticEnv"
