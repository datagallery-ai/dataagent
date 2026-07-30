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
from unittest.mock import patch

from dataagent.agents.nl2sql.nodes.reflector import ReflectorNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import sql_sha256
from dataagent.agents.nl2sql.workflow.state import Result


def _node() -> ReflectorNode:
    node = object.__new__(ReflectorNode)
    node.threshold = 0.9
    node.config = {"threshold": 0.9}
    return node


def test_reflector_stops_loop_on_repeated_sql_but_keeps_sql():
    """Repeated fingerprint stops reflecting; generated SQL remains available."""
    node = _node()
    digest = sql_sha256("SELECT 1")
    best = Result(id=0, sql="SELECT 1", score=0.1, sql_sha256=digest)
    state = {
        "validation_results": [best],
        "seen_sqls": ["SELECT 1"],
        "ref_retries": 2,
        "proceed": False,
        "sql": "SELECT 1",
        "generation_results": [],
        "trace_id": "tid-repeat",
    }
    with patch("dataagent.agents.nl2sql.nodes.reflector.logger.warning") as warn:
        out = ReflectorNode._process(node, state)
    assert out["proceed"] is True
    assert out["sql"] == "SELECT 1"
    warn.assert_called_once()
    msg = warn.call_args.args[0]
    assert "trace_id=tid-repeat" in msg
    assert f"sql_sha256={digest}" in msg


def test_reflector_exhausted_retries_still_proceeds_with_best_sql():
    """Match upstream: retries exhausted still proceeds with best generated SQL."""
    node = _node()
    best = Result(id=0, sql="SELECT bad", score=0.2)
    state = {
        "validation_results": [best],
        "seen_sqls": [],
        "ref_retries": 0,
        "proceed": False,
        "sql": "SELECT bad",
        "generation_results": [],
    }
    out = ReflectorNode._process(node, state)
    assert out["proceed"] is True
    assert out["sql"] == "SELECT bad"


def test_reflector_proceeds_when_score_ok():
    node = _node()
    best = Result(id=0, sql="SELECT 1", score=1.0)
    state = {
        "validation_results": [best],
        "seen_sqls": [],
        "ref_retries": 0,
        "proceed": False,
        "sql": "",
        "generation_results": [],
    }
    out = ReflectorNode._process(node, state)
    assert out["proceed"] is True
    assert out["sql"] == "SELECT 1"
    assert "SELECT 1" in out["seen_sqls"]


def test_reflector_does_not_record_seen_sql_before_proceed():
    """Fingerprint must not enter seen_sqls until this round adopts a passing SQL."""
    node = _node()
    node._fix_sql = lambda _vals: ["SELECT fixed"]  # noqa: ARG005
    low = Result(id=0, sql="SELECT bad", score=0.2, prompt="q")
    state = {
        "validation_results": [low],
        "seen_sqls": [],
        "ref_retries": 2,
        "proceed": False,
        "sql": "SELECT bad",
        "generation_results": [],
        "trace_id": "t1",
    }
    out = ReflectorNode._process(node, state)
    assert out["proceed"] is False
    assert out.get("seen_sqls") == []


def test_reflector_fix_path_clears_residual_error():
    node = _node()
    node._fix_sql = lambda _vals: ["SELECT fixed"]  # noqa: ARG005
    low = Result(id=0, sql="SELECT bad", score=0.2, prompt="q", error="prior execute failed", need_ref=True)
    state = {
        "validation_results": [low],
        "seen_sqls": [],
        "ref_retries": 2,
        "proceed": False,
        "sql": "SELECT bad",
        "generation_results": [],
        "trace_id": "t1",
    }
    out = ReflectorNode._process(node, state)
    assert out["proceed"] is False
    assert low.sql == "SELECT fixed"
    assert low.error is None
