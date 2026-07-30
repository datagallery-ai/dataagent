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
from contextlib import contextmanager
from unittest.mock import MagicMock

from dataagent.agents.nl2sql.errors import SQLServiceError
from dataagent.agents.nl2sql.nodes.executor import ExecutorNode
from dataagent.agents.nl2sql.workflow.state import Result


def _node() -> ExecutorNode:
    node = object.__new__(ExecutorNode)
    node.name = "executor"
    node.limit = -1
    node.preview_limit = 5
    node.config = {}
    node._config_manager = MagicMock()
    node._config_manager.get.side_effect = lambda key, default=None: {
        "DATABASE.config": {},
        "DATABASE.engine": "sqlite",
        "DATABASE.sql_service_engine": "sqlite",
    }.get(key, default)
    return node


def test_executor_catches_sql_service_error(monkeypatch):
    node = _node()
    candidate = Result(id=0, sql="SELECT 1", score=1.0)

    class _Svc:
        def execute(self, sql, **_kwargs):
            raise SQLServiceError(detail="boom")

    @contextmanager
    def _fake_build(_engine, _config):
        yield _Svc()

    monkeypatch.setattr(
        "dataagent.agents.nl2sql.nodes.executor.build_sql_service",
        _fake_build,
    )

    ExecutorNode._process(
        node,
        {
            "validation_results": [candidate],
            "execution_results": [],
            "trace_id": "t1",
        },
    )
    assert candidate.error and "boom" in candidate.error
