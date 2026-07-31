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
"""Integration: live ontology service call via perceive_data_from_ontology.

Requires ``ONTOLOGY_SERVICE_URL``. Without it, the test is skipped.
"""

from __future__ import annotations

import os

import pytest

from dataagent.actions.perceptor.perceptor_atomic import perceive_data_from_ontology
from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.config.config_manager import ConfigManager


@pytest.mark.skipif(
    not os.getenv("ONTOLOGY_SERVICE_URL"),
    reason="ONTOLOGY_SERVICE_URL not set, skipping real ontology search test",
)
def test_perceive_data_from_ontology_real_call():
    ontology_url = os.getenv("ONTOLOGY_SERVICE_URL")
    cm = ConfigManager()
    cm.set("ONTOLOGY_SERVICE_URL", ontology_url)
    ctx = ToolExecutionContext(config_manager=cm)

    query = "查询订单金额相关的本体数据"
    out = perceive_data_from_ontology(query=query, _tool_context=ctx)

    assert "original_msg" in out and isinstance(out["original_msg"], str)
    assert "frontend_msg" in out and isinstance(out["frontend_msg"], str)
    assert "data" in out

    if out["original_msg"] == "Ontology query succeeded.":
        assert isinstance(out["data"], (dict, list, str, int, float, bool))
