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

from dataagent.agents.nl2sql.errors import SQLSecurityValidationError
from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import normalize_sql, process_dimension_joins, quote_sql_placeholders
from dataagent.agents.nl2sql.workflow.state import NL2SQLState, Result
from dataagent.utils.constants import DEFAULT_NL2SQL_REFLECTOR_THRESHOLD
from dataagent.utils.log import logger


def _as_str_list(value: Any) -> list[str]:
    """Normalize a model-supplied field into a list of strings."""
    if isinstance(value, list | tuple):
        return [str(item) for item in value]
    return [str(value)] if value else []


class ReflectorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs):
        super().__init__(name="reflector", **kwargs)
        self.threshold = self.config.get("threshold", DEFAULT_NL2SQL_REFLECTOR_THRESHOLD)
        self.sql_security_enabled = self.config.get("sql_security_enabled", True)

    async def _aprocess(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        _ = runtime
        safe_results = [
            result
            for result in state["validation_results"]
            if not result.security_violations and (result.security_checked or not self.sql_security_enabled)
        ]
        if not safe_results and state["ref_retries"] <= 0:
            violations = []
            for result in state["validation_results"]:
                violations.extend(result.security_violations)
            raise SQLSecurityValidationError(violations=violations)
        best = max(safe_results or state["validation_results"], key=lambda result: result.score)
        if safe_results and ((best.score >= self.threshold and not best.need_ref) or state["ref_retries"] <= 0):
            return self._approve(state, safe_results, best)
        state["ref_retries"] -= 1
        state["proceed"] = False
        state["security_sql_approved"] = False
        history = state.setdefault("review_history", [])
        reviewed_before = bool(history)
        repaired = None
        for _ in range(3):
            out = await self._fix_sql(state["validation_results"], history)
            if len(out) == len(state["validation_results"]):
                repaired = out
                break
        if repaired is None:
            # skip if fail
            logger.warning("Reflector failed.")
            fix_sqls = [v.sql for v in state["validation_results"]]
        else:
            fix_sqls = [fix["sql"] for fix in repaired]
            changed = self._record_review_round(history, state["validation_results"], repaired)
            # The first unchanged round still goes back, so the Validator can read this
            # round's `unresolved` report before the loop gives up.
            if reviewed_before and safe_results and not best.need_ref and not any(changed):
                logger.info("Reflector stopped: SQL unchanged across rounds.")
                return self._approve(state, safe_results, best)
        current_batch = []
        for v, sql in zip(state["validation_results"], fix_sqls, strict=True):
            v.sql, v.score, v.issues, v.need_ref = sql, 0, [], False
            v.security_checked = False
            v.security_violations = []
            current_batch.append(v)
            state["generation_results"].append(v)
        state["validation_results"].clear()
        if self._config_manager is not None and current_batch:
            rewritten = await process_dimension_joins(
                [(item.sql, item.prompt, item.strategy) for item in current_batch],
                state,
                scenario=self._get_agent_config("DATABASE.perceptor_type", ""),
                dialect=self.dialect,
                execute_with_llm=self.execute_dimension_join_llm,
            )
            for original, new in zip(current_batch, rewritten, strict=True):
                original.sql = new.sql
                original.prompt = new.prompt
                original.need_ref = new.need_ref
        p = "\n".join([s.sql for s in state["generation_results"]])
        message = f"=== Reflector ===\n{p}"
        logger.info(message)
        state["stream_message"] = message
        return state

    def _approve(self, state: NL2SQLState, safe_results: list[Result], best: Result) -> NL2SQLState:
        """Accept ``best`` as the final SQL and let the workflow move past reflection."""
        state["validation_results"] = safe_results
        state["proceed"] = True
        state["sql"] = best.sql
        state["security_sql_approved"] = True
        return state

    def _record_review_round(
        self, history: list[dict[str, Any]], results: list[Result], fixes: list[dict[str, Any]]
    ) -> list[bool]:
        """Append this round to the ledger and return which candidates actually changed."""
        round_index = max((entry.get("round", 0) for entry in history), default=0) + 1
        changed = []
        for v, fix in zip(results, fixes, strict=True):
            is_changed = normalize_sql(v.sql) != normalize_sql(fix["sql"])
            changed.append(is_changed)
            history.append(
                {
                    "round": round_index,
                    "id": v.id,
                    "sql_before": v.sql,
                    "issues": list(v.issues),
                    "sql_after": fix["sql"],
                    "changed": is_changed,
                    "unresolved": list(fix["unresolved"]),
                }
            )
        return changed

    async def _fix_sql(
        self, val_res: list[Result], review_history: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        cases = json.dumps(
            [{"id": v.id, "sql": v.sql, "issues": v.issues} for v in val_res],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        history = json.dumps(review_history, ensure_ascii=False, separators=(",", ":")) if review_history else ""
        context = {"cases": cases, "prompt": val_res[0].prompt, "review_history": history}
        response = await self.execute_with_llm_json(context)
        return [
            {"sql": quote_sql_placeholders(x["sql"]), "unresolved": _as_str_list(x.get("unresolved"))} for x in response
        ]
