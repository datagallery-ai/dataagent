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

from dataagent.core.context.context import ContextFactory
from dataagent.utils.converter.result_ir_converter import ResultIRConverter


@pytest.fixture(autouse=True)
def _clear_context_factory() -> None:
    ContextFactory.clear_context()
    yield
    ContextFactory.clear_context()


def test_ir_converter_file_fallback_uses_custom_tool_outputs_dir(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    context = ContextFactory.get_context(
        user_id="u1",
        session_id="s1",
        run_id=0,
        sub_id=0,
    )
    context.state.config = {"WORKSPACE_POLICY": {"layout": {"tool_outputs_dir": ".xxtool_outputs/custom/"}}}
    context.register_query(query="test", additional_files=[])
    context.register_node(
        node_type="Action",
        label="act001",
        description="",
        predecessor_node=["Query(query00000)"],
        action="bash",
        params={},
        output="Pending",
        success=False,
    )

    created = ResultIRConverter._create_file_fallback(
        context,
        "x" * 600,
        "Action(act001)",
        "bash",
        workspace,
        knowledge_min_length=500,
    )

    output_dir = workspace / ".xxtool_outputs" / "custom"
    assert output_dir.is_dir()
    assert any(output_dir.iterdir())
    assert not (workspace / ".dataagent").exists()
    assert created
