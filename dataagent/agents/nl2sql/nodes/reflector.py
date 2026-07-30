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
from dataagent.agents.nl2sql.utils.nl2sql_utils import normalize_sql, quote_sql_placeholders, sql_sha256
from dataagent.agents.nl2sql.workflow.state import NL2SQLState, Result
from dataagent.utils.constants import DEFAULT_NL2SQL_REFLECTOR_THRESHOLD
from dataagent.utils.log import logger


class ReflectorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs):
        super().__init__(name="reflector", **kwargs)
        self.threshold = self.config.get("threshold", DEFAULT_NL2SQL_REFLECTOR_THRESHOLD)

    def _process(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        best = max(state["validation_results"], key=lambda r: r.score)
        seen = list(state.get("seen_sqls") or [])
        fingerprint = normalize_sql(best.sql)

        # Same-fingerprint loop break: stop reflecting but keep the generated SQL
        # on the happy path (generation-only clients still get an answer).
        trace_id = state.get("trace_id") or ""
        best_digest = best.sql_sha256 or sql_sha256(best.sql)

        if fingerprint and fingerprint in seen:
            logger.warning(
                f"Reflector: repeated SQL fingerprint; stop reflecting and proceed. "
                f"trace_id={trace_id} sql_sha256={best_digest}"
            )
            state["proceed"] = True
            state["sql"] = best.sql
            return state

        # Match upstream: threshold pass OR retries exhausted → proceed with best.sql.
        if (best.score >= self.threshold and not best.need_ref) or state["ref_retries"] <= 0:
            state["proceed"] = True
            state["sql"] = best.sql
            if fingerprint and best.score >= self.threshold and not best.need_ref:
                seen.append(fingerprint)
                state["seen_sqls"] = seen
            logger.info(f"=== Reflector ===\ntrace_id={trace_id} sql_sha256={best_digest} proceed=True")
            return state

        state["ref_retries"] -= 1
        state["proceed"] = False
        for _ in range(3):
            out = self._fix_sql(state["validation_results"])
            if len(out) == len(state["validation_results"]):
                fix_sqls = out
                break
        else:
            # skip if fail
            logger.warning(f"Reflector failed. trace_id={trace_id}")
            fix_sqls = [v.sql for v in state["validation_results"]]
        for v, sql in zip(state["validation_results"], fix_sqls, strict=True):
            v.sql, v.score, v.issues, v.need_ref, v.error = sql, 0, [], False, None
            v.sql_sha256 = sql_sha256(sql)
            if not v.trace_id:
                v.trace_id = trace_id
            state["generation_results"].append(v)
        state["validation_results"].clear()
        p = "\n".join([f"sql_sha256={s.sql_sha256}\n{s.sql}" for s in state["generation_results"]])
        message = f"=== Reflector ===\ntrace_id={trace_id}\n{p}"
        logger.info(message)
        state["stream_message"] = message
        return state

    def _fix_sql(self, val_res: list[Result]) -> list[str]:
        cases = [{"id": v.id, "sql": v.sql, "issues": v.issues} for v in val_res]
        cases = json.dumps(cases, ensure_ascii=False, separators=(",", ":"))
        context = {"cases": cases, "prompt": val_res[0].prompt}
        return [quote_sql_placeholders(x["sql"]) for x in self.execute_with_llm_json(context)]
