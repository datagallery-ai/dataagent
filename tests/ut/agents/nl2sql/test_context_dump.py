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
from pathlib import Path

from dataagent.agents.nl2sql.agent import NL2SQLAgent
from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.utils.runtime_paths import resolve_flex_session_memory_dir


def test_context_dump_uses_same_memory_dir_as_flex(monkeypatch, tmp_path: Path) -> None:
    """Place NL2SQL prompt dumps under the same session memory directory as Flex."""
    monkeypatch.setenv("DATAAGENT_CONTEXT_DUMP", "1")
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))
    workspace = tmp_path / "custom-workspace"
    state = {
        "user_id": "user-1",
        "session_id": "session-1",
        "run_id": 3,
        "workspace": str(workspace),
    }
    node = BaseNL2SQLNode(name="generator")
    agent = object.__new__(NL2SQLAgent)
    agent._config_obj = {}
    agent.config = {}
    agent.nodes = [node]

    agent._distribute_context_dump_dir(state, session_id="session-1")

    flex_memory_dir = resolve_flex_session_memory_dir(
        user_id="user-1",
        session_id="session-1",
        workspace=workspace,
        config={},
    )
    assert node._nl2sql_context_dump_dir == flex_memory_dir / "context_dump" / "run_3" / "nl2sql_01"
