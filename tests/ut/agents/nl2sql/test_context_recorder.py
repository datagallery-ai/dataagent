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
import json

import networkx as nx

from dataagent.agents.nl2sql.context_recorder import NL2SQLContextRecorder
from dataagent.core.context.context import ContextFactory


class TestNL2SQLContextRecorder:
    """Verify minimal NL2SQL trajectory recording without running an Agent."""

    def setup_method(self) -> None:
        """Clear cached Context instances before each test."""
        ContextFactory.clear_context()

    def teardown_method(self) -> None:
        """Clear cached Context instances after each test."""
        ContextFactory.clear_context()

    def test_records_query_actions_and_response(self, tmp_path) -> None:
        """Persist the completed node sequence as one acyclic Context graph."""
        state = {
            "user_id": "u1",
            "session_id": "s1",
            "run_id": 2,
            "sub_id": 5,
            "workspace": str(tmp_path),
            "sql": "SELECT 1",
        }
        recorder = NL2SQLContextRecorder.create(
            state=state,
            question="return one",
            session_id=None,
            config={},
        )

        assert recorder is not None
        token = recorder.bind()
        try:
            assert NL2SQLContextRecorder.record_action_hook(state, node_name="perceptor") is state
            NL2SQLContextRecorder.record_action_hook(state, node_name="generator")
            recorder.finish(final_state=state, completed=True)
        finally:
            recorder.reset(token)

        context_path = tmp_path / ".context" / "Run2_Sub5.json"
        data = json.loads(context_path.read_text(encoding="utf-8"))
        graph = nx.node_link_graph(data=data, edges="edges")
        node_types = [attrs.get("node_type") for _, attrs in graph.nodes(data=True)]
        actions = [attrs.get("action") for _, attrs in graph.nodes(data=True) if attrs.get("node_type") == "Action"]

        assert node_types == ["Query", "Action", "Action", "Response"]
        assert actions == ["perceptor", "generator"]
        assert nx.is_directed_acyclic_graph(graph)

    def test_incomplete_run_persists_partial_graph_without_response(self, tmp_path) -> None:
        """Persist completed actions but omit Response when the workflow fails."""
        state = {
            "user_id": "u1",
            "session_id": "s1",
            "run_id": 0,
            "sub_id": 0,
            "workspace": str(tmp_path),
        }
        recorder = NL2SQLContextRecorder.create(
            state=state,
            question="will fail",
            session_id=None,
            config={},
        )

        assert recorder is not None
        token = recorder.bind()
        try:
            NL2SQLContextRecorder.record_action_hook(state, node_name="perceptor")
            recorder.finish(final_state=None, completed=False)
        finally:
            recorder.reset(token)

        context_path = tmp_path / ".context" / "Run0_Sub0.json"
        data = json.loads(context_path.read_text(encoding="utf-8"))
        graph = nx.node_link_graph(data=data, edges="edges")
        node_types = [attrs.get("node_type") for _, attrs in graph.nodes(data=True)]

        assert node_types == ["Query", "Action"]
