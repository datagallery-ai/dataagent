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
import asyncio
from typing import Any, Optional, cast

from dataagent.agents.nl2sql.errors import SQLServiceError
from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import truncate
from dataagent.agents.nl2sql.utils.sql_service import build_sql_service
from dataagent.agents.nl2sql.workflow.state import NL2SQLState
from dataagent.core.cbb.base_state import BaseState
from dataagent.utils.constants import DEFAULT_NL2SQL_PREVIEW_LIMIT
from dataagent.utils.log import logger

_ExecutionOutput = tuple[Optional[list[str]], Optional[list[tuple[Any, ...]]], Optional[str]]  # noqa: UP045


class ExecutorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs):
        super().__init__(name="executor", **kwargs)
        self.limit = kwargs.pop("limit", -1)
        self.preview_limit = kwargs.pop("preview_limit", DEFAULT_NL2SQL_PREVIEW_LIMIT)

    async def _aprocess(self, state: BaseState, runtime: Any = None) -> NL2SQLState:
        state = cast(NL2SQLState, state)
        config = self._get_agent_config("DATABASE.config", {}) or {}
        state["execution_results"] = []
        p = []
        validation_results = state["validation_results"]
        outputs = await asyncio.to_thread(self._execute_queries, config, [result.sql for result in validation_results])
        for v, (columns, rows, error) in zip(validation_results, outputs, strict=True):
            v.columns, v.error = columns, error
            state["execution_results"].append(v)
            if v.error and rows is None:
                v.rows, v.rows_preview = None, None
                p.append(str(v.error))
                continue
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
            if v.rows and v.rows_preview and len(v.rows) > len(v.rows_preview):
                p[-1] += f" ... and {len(v.rows) - len(v.rows_preview)} more rows"
        state["validation_results"].clear()
        trace_id = state.get("trace_id") or ""
        digests = ",".join(f"{v.id}:{v.sql_sha256}" for v in state["execution_results"] if getattr(v, "sql_sha256", ""))
        result_preview = "\n".join(p)
        message = f"=== Executor ===\ntrace_id={trace_id} sql_sha256=[{digests}]\n{result_preview}"
        logger.info(message)
        state["stream_message"] = message
        return state

    def _execute_queries(self, config: dict[str, Any], sqls: list[str]) -> list[_ExecutionOutput]:
        outputs: list[_ExecutionOutput] = []
        with build_sql_service(self.sql_service_engine, config) as service:
            for sql in sqls:
                try:
                    outputs.append(service.execute(sql))
                except SQLServiceError as exc:
                    outputs.append((None, None, str(exc.detail or exc.message or exc)))
        return outputs
