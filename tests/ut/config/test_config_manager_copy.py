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
"""ConfigManager.copy() independence tests for per-Agent isolation."""

from pathlib import Path

from dataagent.config.config_manager import ConfigManager


class TestConfigManagerCopy:
    """Verify copy() produces an independent ConfigManager instance."""

    def test_copy_is_independent_of_nested_mutations(self):
        """Mutating nested settings on a copy must not affect the original."""
        cm = ConfigManager()
        cm.set("DATABASE", {"db_id": "ORIGINAL"})
        cm.config_path = Path("/tmp/original.yaml")

        copied = cm.copy()
        copied.set("DATABASE.db_id", "MUTATED")

        assert cm.get("DATABASE.db_id") == "ORIGINAL"
        assert copied.get("DATABASE.db_id") == "MUTATED"

    def test_copy_preserves_config_path(self):
        """copy() should retain config_path for DataAgent ownership semantics."""
        cm = ConfigManager()
        cm.config_path = Path("/tmp/agent_a.yaml")
        copied = cm.copy()
        assert copied.config_path == Path("/tmp/agent_a.yaml")

    def test_copy_settings_is_deep_snapshot(self):
        """get_all() on copy must not share mutable dict references with original."""
        cm = ConfigManager()
        cm.set("NESTED", {"k": 1})
        copied = cm.copy()
        all_copied = copied.get_all()
        all_copied["NESTED"]["k"] = 99
        assert cm.get("NESTED.k") == 1
