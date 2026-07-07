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
from dataagent.agents.nl2sql.nodes import executor as executor_module
from dataagent.agents.nl2sql.nodes.executor import ExecutorNode
from dataagent.agents.nl2sql.nodes.selector import SelectorNode
from dataagent.agents.nl2sql.workflow.state import Result, get_default_state


class _ConfigManager:
    def get(self, key, default=None):
        return default


class _SQLService:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql):
        return ["secret_column"], [("secret-value",)], None


class _SelectorNode(SelectorNode):
    def execute_with_llm(self, context, action=""):
        return "not json"


def test_executor_does_not_log_sql_result_preview(monkeypatch):
    logged_messages = []

    class _Logger:
        @staticmethod
        def info(message, *args):
            logged_messages.append(message.format(*args))

    monkeypatch.setattr(executor_module, "logger", _Logger())
    monkeypatch.setattr(executor_module, "build_sql_service", lambda *args, **kwargs: _SQLService())
    state = get_default_state("question", validation_results=[Result(id=1, sql="SELECT secret_column FROM t")])

    out = ExecutorNode(config_manager=_ConfigManager())._process(state)

    assert "secret-value" in out["stream_message"]
    assert logged_messages == ["=== Executor === result_count=1"]


def test_selector_falls_back_when_llm_output_parse_fails():
    state = get_default_state(
        "question",
        execution_results=[
            Result(
                id=1,
                sql="SELECT 1",
                columns=["col"],
                rows=[(1,)],
                rows_preview=[("1",)],
            )
        ],
    )

    out = _SelectorNode()._process(state)

    assert out["confidence"] == 1
    assert out["sql"] == "SELECT 1"
