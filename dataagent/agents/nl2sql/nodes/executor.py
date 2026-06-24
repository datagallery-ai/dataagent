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
from typing import Any

from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import truncate
from dataagent.agents.nl2sql.utils.sql_service import build_sql_service
from dataagent.agents.nl2sql.workflow.state import NL2SQLState
from dataagent.utils.constants import DEFAULT_NL2SQL_PREVIEW_LIMIT
from dataagent.utils.log import logger


class ExecutorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs):
        super().__init__(name="executor", **kwargs)
        self.limit = kwargs.pop("limit", -1)
        self.preview_limit = kwargs.pop("preview_limit", DEFAULT_NL2SQL_PREVIEW_LIMIT)

    def _process(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        self._trajectory_recorder.record_node_start(
            node_name="executor",
            purpose=f"Execute SQL candidates against database engine: {self.sql_service_engine}",
        )
        config = self._get_agent_config("DATABASE.config", {}) or {}
        state["execution_results"] = []
        p = []
        with build_sql_service(self.sql_service_engine, config) as service:
            for v in state["validation_results"]:
                tid = self._trajectory_recorder.record_tool_call(
                    tool_name="sql_execute",
                    args={"sql": v.sql, "engine": self.sql_service_engine},
                    purpose=f"Execute SQL candidate #{v.id} against {self.sql_service_engine} database",
                )
                v.columns, rows, v.error = service.execute(v.sql)
                exec_result = f"columns={v.columns}, rows_count={len(rows) if rows else 0}, error={v.error}"
                self._trajectory_recorder.record_tool_result(content=exec_result, tool_call_id=tid)
                state["execution_results"].append(v)
                v.rows = None if rows is None else (rows[: self.limit] if self.limit >= 0 else rows)
                v.rows_preview = (
                    None
                    if rows is None
                    else [
                        tuple(truncate(x) for x in r)
                        for r in (rows[: self.preview_limit] if self.preview_limit >= 0 else rows)
                    ]
                )
                p.append(str(v.rows_preview))
                if v.rows and len(v.rows) > len(v.rows_preview):
                    p[-1] += f" ... and {len(v.rows) - len(v.rows_preview)} more rows"
        state["validation_results"].clear()
        result_preview = "\n".join(p)
        message = f"=== Executor ===\n{result_preview}"
        logger.info(message)
        state["stream_message"] = message
        return state
