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
"""Tests for SWARM YAML validation in ``ConfigManager.reload``."""

import pytest

from dataagent.config.config_manager import ConfigManager


@pytest.mark.parametrize(
    ("settings", "expect_ok"),
    [
        ({}, True),
        ({"SWARM": {}}, True),
        ({"SWARM": {"worker_max_concurrent": None}}, True),
        ({"SWARM": {"worker_max_concurrent": 0}}, True),
        ({"SWARM": {"worker_max_concurrent": 1}}, True),
        ({"SWARM": {"worker_max_concurrent": True}}, False),
        ({"SWARM": {"worker_max_concurrent": False}}, False),
        ({"SWARM": {"worker_max_concurrent": -1}}, False),
        ({"SWARM": {"worker_max_concurrent": "2"}}, False),
        ({"SWARM": {"worker_max_concurrent": 2.0}}, False),
        ({"SWARM": {"worker_max_concurrent": []}}, False),
    ],
)
def test_validate_swarm_worker_max_concurrent(settings: dict, expect_ok: bool) -> None:
    if expect_ok:
        ConfigManager._validate_swarm_yaml_config(settings)
    else:
        with pytest.raises(ValueError, match="SWARM.worker_max_concurrent"):
            ConfigManager._validate_swarm_yaml_config(settings)
