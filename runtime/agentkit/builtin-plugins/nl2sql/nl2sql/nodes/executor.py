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
from typing import Any

from loguru import logger

from nl2sql.constants import DEFAULT_NL2SQL_PREVIEW_LIMIT
from nl2sql.errors import DataAgentError
from nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from nl2sql.state import NL2SQLState
from nl2sql.utils.nl2sql_utils import truncate
from nl2sql.utils.sql_service import build_sql_service

_ExecutionOutput = tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]  # noqa: UP045


class ExecutorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs):
        super().__init__(name="executor", **kwargs)
        self.limit = kwargs.pop("limit", -1)
        self.preview_limit = kwargs.pop("preview_limit", DEFAULT_NL2SQL_PREVIEW_LIMIT)

    async def _aprocess(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        _ = runtime
        config = self._config.connection_config()
        state["execution_results"] = []
        p = []
        validation_results = state["validation_results"]
        outputs = await asyncio.to_thread(
            self._execute_queries, config, [result.sql for result in validation_results], self.limit
        )
        for v, (columns, rows, error) in zip(validation_results, outputs, strict=True):
            v.columns, v.error = columns, error
            state["execution_results"].append(v)
            if rows is None:
                v.rows, v.rows_preview = None, None
                v.row_count, v.total_known, v.truncated = 0, False, False
            else:
                # The service fetches at most limit+1 rows, so one extra row flags truncation.
                v.truncated = self.limit >= 0 and len(rows) > self.limit
                v.rows = rows[: self.limit] if self.limit >= 0 else rows
                v.row_count = len(v.rows)
                # Within the fetch cap the result is complete; only truncation makes it partial.
                v.total_known = not v.truncated
                v.rows_preview = [
                    tuple(truncate(x) for x in r)
                    for r in (v.rows[: self.preview_limit] if self.preview_limit >= 0 else v.rows)
                ]
            p.append(str(v.rows_preview))
            if v.rows and v.rows_preview and len(v.rows) > len(v.rows_preview):
                p[-1] += f" ... and {len(v.rows) - len(v.rows_preview)} more rows"
        state["validation_results"].clear()
        result_preview = "\n".join(p)
        message = f"=== Executor ===\n{result_preview}"
        logger.info(message)
        state["stream_message"] = message
        return state

    def _execute_queries(self, config: dict[str, Any], sqls: list[str], limit: int) -> list[_ExecutionOutput]:
        outputs: list[_ExecutionOutput] = []
        with build_sql_service(self.engine, config) as service:
            for sql in sqls:
                try:
                    outputs.append(service.execute(sql, limit=limit))
                except Exception as e:
                    outputs.append((None, None, _candidate_error_text(e)))
        return outputs


def _candidate_error_text(exc: Exception) -> str:
    """Keep going to Selector; surface a readable error instead of stopping the workflow."""
    if isinstance(exc, DataAgentError):
        return exc.fact
    return str(exc)
