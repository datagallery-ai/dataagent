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
"""Swarm config helpers must accept explicit config, not read global config_manager."""

from dataagent.core.swarm.swarm_config import swarm_enabled, swarm_worker_max_concurrent


class TestSwarmConfigExplicit:
    """swarm_* helpers read from passed config mapping only."""

    def test_swarm_enabled_false(self):
        """Explicit SWARM.enable=False returns False."""
        assert swarm_enabled({"SWARM": {"enable": False}}) is False

    def test_swarm_enabled_true(self):
        """Explicit SWARM.enable=True returns True."""
        assert swarm_enabled({"SWARM": {"enable": True}}) is True

    def test_swarm_enabled_defaults_false_when_missing(self):
        """Missing SWARM.enable defaults to False; only explicit enable=True turns swarm on."""
        assert swarm_enabled({}) is False
        assert swarm_enabled({"SWARM": {}}) is False
        assert swarm_enabled({"SWARM": {"worker_max_concurrent": 2}}) is False

    def test_swarm_worker_max_concurrent(self):
        """Explicit worker_max_concurrent is parsed as positive int."""
        assert swarm_worker_max_concurrent({"SWARM": {"worker_max_concurrent": 2}}) == 2

    def test_swarm_worker_max_concurrent_missing_returns_none(self):
        """Missing worker_max_concurrent returns None (no cap)."""
        assert swarm_worker_max_concurrent({}) is None
        assert swarm_worker_max_concurrent({"SWARM": {}}) is None

    def test_swarm_worker_max_concurrent_invalid_returns_none(self):
        """Invalid values are treated as no cap."""
        assert swarm_worker_max_concurrent({"SWARM": {"worker_max_concurrent": 0}}) is None
        assert swarm_worker_max_concurrent({"SWARM": {"worker_max_concurrent": -1}}) is None
        assert swarm_worker_max_concurrent({"SWARM": {"worker_max_concurrent": "bad"}}) is None
