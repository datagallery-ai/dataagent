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
from typing import Any

from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import json_parser
from dataagent.agents.nl2sql.workflow.state import NL2SQLState, Result
from dataagent.utils.constants import DEFAULT_NL2SQL_REF_RETRIES, DEFAULT_NL2SQL_SELECTOR_THRESHOLD
from dataagent.utils.log import logger


class SelectorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs):
        super().__init__(name="selector", **kwargs)
        self.threshold = self.config.get("threshold", DEFAULT_NL2SQL_SELECTOR_THRESHOLD)
        self.shortcut = self.config.get("shortcut", -1)

    def _process(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        best = None
        if self.shortcut >= 0:
            best, vote = self._vote(state["execution_results"])
            if best:
                best.confidence = 1.0
                p = f"Shortcut with {vote} votes: {best.sql}"
        if not best:
            res = [
                {"id": r.id, "sql": r.sql, "cols": r.columns, "rows": r.rows_preview, "err": r.error}
                for r in state["execution_results"]
            ]
            context = {
                "schema": state["schema_str"],
                "question": state["question"],
                "sql_rules": state["sql_rules"],
                "res": json.dumps(res, default=str),
            }
            for _ in range(3):
                out = json.loads(json_parser(self.execute_with_llm(context)))
                if len(out) == len(state["execution_results"]):
                    sel = out
                    for r in res:
                        del r["id"]
                    break
            else:
                # skip if fail
                logger.warning("Selector failed.")
                sel = [{"score": 1, "issues": []}] * len(state["execution_results"])
            for e, s in zip(state["execution_results"], sel, strict=True):
                e.confidence = s["score"]
                e.issues = e.issues + s["issues"]
            best = max(state["execution_results"], key=lambda e: (e.confidence, e.score))
            p = "\n".join([f"Score: {e.confidence:.2f}, Issues: {e.issues}" for e in state["execution_results"]])
        message = f"=== Selector ===\n{p}"
        logger.info(message)
        state["stream_message"] = message
        if best.confidence >= self.threshold or state["sel_retries"] <= 0:
            state["sql"], state["confidence"] = best.sql, best.confidence
            state["columns"], state["rows"], state["rows_preview"] = best.columns, best.rows, best.rows_preview
            p = f"{state['sql']}\n{state['rows_preview']}"
            if best.rows and len(best.rows) > len(best.rows_preview):
                p += f" ... and {len(best.rows) - len(best.rows_preview)} more rows"
            message = f"=== Final Result ===\n{p}"
            logger.info(message)
            state["stream_message"] = message
            return state
        state["ref_retries"] = self._get_agent_config("CORE.reflector.ref_retries", DEFAULT_NL2SQL_REF_RETRIES)
        state["sel_retries"] -= 1
        for e in state["execution_results"]:
            e.need_ref = True
        state["proceed"], state["validation_results"] = False, list(state["execution_results"])
        state["execution_results"].clear()
        return state

    def _vote(self, res: list[Result]) -> tuple[Result, int] | tuple[None, int]:
        result_map = {}
        for r in res:
            if r.error or r.columns is None or r.rows is None:
                continue
            key = frozenset(r.rows)
            if key not in result_map:
                result_map[key] = {"vote": 1, "result": r}
                continue
            candidate = result_map[key]
            candidate["vote"] += 1
            if len(r.sql) < len(candidate["result"].sql):
                candidate["result"] = r
        if not result_map or max(result_map.values(), key=lambda x: x["vote"])["vote"] < self.shortcut:
            return None, 0
        best = max(result_map.values(), key=lambda x: x["vote"])
        return best["result"], best["vote"]
