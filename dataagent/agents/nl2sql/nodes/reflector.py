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
import re
from typing import Any

from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import json_parser, quote_sql_placeholders
from dataagent.agents.nl2sql.workflow.state import NL2SQLState, Result
from dataagent.utils.constants import DEFAULT_NL2SQL_REFLECTOR_THRESHOLD
from dataagent.utils.log import logger

_COLUMN_ERROR_PATTERN = re.compile(r"no such column", re.IGNORECASE)


class ReflectorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs):
        super().__init__(name="reflector", **kwargs)
        self.threshold = self.config.get("threshold", DEFAULT_NL2SQL_REFLECTOR_THRESHOLD)

    def _is_only_column_errors(self, results: list[Result]) -> bool:
        all_issues = []
        for r in results:
            all_issues.extend(r.issues)
        if not all_issues:
            return False
        return all(_COLUMN_ERROR_PATTERN.search(i) for i in all_issues)

    def _process(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        if not state["validation_results"]:
            logger.warning("Reflector: empty validation_results, accepting without repair.")
            state["proceed"] = True
            return state
        best = max(state["validation_results"], key=lambda r: r.score)
        schema_str = state.get("schema_str", "")
        logger.debug(
            f"Reflector received schema_str: len={len(schema_str)}, preview={schema_str[:80] if schema_str else 'EMPTY'}"
        )
        if (best.score >= self.threshold and not best.need_ref) or state["ref_retries"] <= 0:
            state["proceed"] = True
            state["sql"] = best.sql
            return state
        if not schema_str and self._is_only_column_errors(state["validation_results"]):
            logger.warning(
                "Reflector: schema_str is empty and all issues are column errors; cannot fix without schema, accepting best result."
            )
            state["proceed"] = True
            state["sql"] = best.sql
            return state
        state["ref_retries"] -= 1
        state["proceed"] = False
        fix_sqls: list[str] = [v.sql for v in state["validation_results"]]
        for _ in range(3):
            out = self._fix_sql(state["validation_results"], state["schema_str"])
            if len(out) == len(state["validation_results"]):
                fix_sqls = out
                break
        else:
            # skip if fail
            logger.warning("Reflector failed.")
        for v, sql in zip(state["validation_results"], fix_sqls, strict=True):
            v.sql, v.score, v.issues, v.need_ref = sql, 0, [], False
            state["generation_results"].append(v)
        state["validation_results"].clear()
        p = "\n".join([s.sql for s in state["generation_results"]])
        message = f"=== Reflector ===\n{p}"
        logger.info(message)
        state["stream_message"] = message
        return state

    def _fix_sql(self, val_res: list[Result], schema_str: str) -> list[str]:
        cases = [{"id": v.id, "sql": v.sql, "issues": v.issues} for v in val_res]
        cases = json.dumps(cases, ensure_ascii=False, separators=(",", ":"))
        context = {"cases": cases, "prompt": val_res[0].prompt, "schema": schema_str}
        return [quote_sql_placeholders(x["sql"]) for x in json.loads(json_parser(self.execute_with_llm(context)))]
