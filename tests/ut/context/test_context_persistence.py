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

from dataagent.core.context.context import ContextFactory


class TestContextPersistence:
    def setup_method(self) -> None:
        ContextFactory.clear_context()

    def teardown_method(self) -> None:
        ContextFactory.clear_context()

    def test_persist_to_json_writes_single_trajectory_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "dataagent.core.context.context_persistence.resolve_session_root",
            lambda **_: tmp_path,
        )
        ctx = ContextFactory.get_context(user_id="u1", session_id="s1", run_id=0, sub_id=0)
        ctx.register_query(query="hello", additional_files=[])
        ctx.register_node(
            node_type="Action",
            description="do something",
            action="Tool(x)",
            params={},
            output="ok",
            success=True,
            predecessor_node=[ctx.initial_pt or "Query(query00000)"],
        )

        path = ctx.persist_to_json()
        assert path.endswith("Run0_Sub0.json")

        context_dir = tmp_path / ".context"
        json_files = list(context_dir.glob("*.json"))
        assert json_files == [context_dir / "Run0_Sub0.json"]

        data = json.loads((context_dir / "Run0_Sub0.json").read_text(encoding="utf-8"))
        graph = nx.node_link_graph(data=data, edges="edges")
        assert graph.number_of_nodes() >= 2

    def test_restore_previous_runs_from_trajectory_json(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "dataagent.core.context.context_persistence.resolve_session_root",
            lambda **_: tmp_path,
        )

        run0 = ContextFactory.get_context(user_id="u1", session_id="s1", run_id=0, sub_id=0)
        run0.register_query(query="turn 0", additional_files=[])
        run0.persist_to_json()
        ContextFactory.clear_context()

        run1 = ContextFactory.get_context(user_id="u1", session_id="s1", run_id=1, sub_id=0)
        run1.restore_previous_runs(user_id="u1", session_id="s1", current_run_id=1, sub_id=0)

        assert 0 in run1.get_all_historical_trajectories()
        assert run1.get_all_historical_trajectories()[0].number_of_nodes() >= 1
        merged = run1.get_trajectory(trimmed=False)
        assert any(str(n).startswith("Query(") for n in merged.nodes)
