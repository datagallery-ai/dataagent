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

from dataagent.agents.bird.constants import DEFAULT_BIRD_REFLECTOR_THRESHOLD
from dataagent.agents.bird.errors import SQLSecurityValidationError
from dataagent.agents.bird.nodes.base_bird_node import BaseBirdNode
from dataagent.agents.bird.security import check_sql
from dataagent.agents.bird.utils.bird_utils import quote_sql_placeholders
from dataagent.agents.bird.utils.sql_validation import validate_single_read_only_sql
from dataagent.agents.bird.utils.structured_json import REFLECTOR_RESULTS_SCHEMA, validate_reflector_results
from dataagent.agents.bird.workflow.state import BirdState, Result
from dataagent.utils.log import logger


class ReflectorNode(BaseBirdNode):
    def __init__(self, **kwargs):
        super().__init__(name="reflector", **kwargs)
        self.threshold = self.config.get("threshold", DEFAULT_BIRD_REFLECTOR_THRESHOLD)
        self.sql_security_enabled = bool(self.config.get("sql_security_enabled", False))
        self.single_statement_only = True

    async def _aprocess(self, state: BirdState, runtime: Any = None) -> BirdState:
        _ = runtime
        safe_results = self._safe_validation_results(state)
        self._raise_if_no_safe_results(state, safe_results)
        best = max(safe_results or state["validation_results"], key=lambda result: result.score)
        approved_results = [result for result in safe_results if result.score >= self.threshold]
        may_proceed = bool(approved_results)
        if safe_results and (may_proceed or state["ref_retries"] <= 0):
            return self._accept_results(state, safe_results, approved_results, best)
        state["ref_retries"] -= 1
        state["proceed"] = False
        state["security_sql_approved"] = False
        fix_sqls = await self._request_repairs(state["validation_results"])
        for result, sql in zip(state["validation_results"], fix_sqls, strict=True):
            self._apply_repair(state, result, sql)
        return self._finish_repairs(state)

    def _safe_validation_results(self, state: BirdState) -> list[Result]:
        return [
            result
            for result in state["validation_results"]
            if not result.security_violations and (result.security_checked or not self.sql_security_enabled)
        ]

    def _raise_if_no_safe_results(self, state: BirdState, safe_results: list[Result]) -> None:
        if safe_results or state["ref_retries"] > 0:
            return
        rule_id_set = set()
        for result in state["validation_results"]:
            for violation in result.security_violations:
                rule_id = violation.get("rule_id", "")
                if rule_id:
                    rule_id_set.add(rule_id)
        rule_ids = sorted(rule_id_set)
        detail = f"Blocked by SQL security rules: {', '.join(rule_ids)}" if rule_ids else "No safe SQL candidate."
        raise SQLSecurityValidationError(detail=detail)

    def _accept_results(
        self,
        state: BirdState,
        safe_results: list[Result],
        approved_results: list[Result],
        best: Result,
    ) -> BirdState:
        for result in approved_results:
            result.validation_passed = True
        state["validation_results"] = safe_results
        state["proceed"] = True
        state["sql"] = best.sql
        state["security_sql_approved"] = True
        return state

    async def _request_repairs(self, validation_results: list[Result]) -> list[str]:
        return await self._fix_sql(validation_results)

    def _apply_repair(self, state: BirdState, result: Result, sql: str) -> None:
        blocked_repair = None
        if self.sql_security_enabled:
            security = check_sql(sql, dialect=self.dialect, schema=state["schema"])
            if security.blocked:
                rule_ids = sorted({violation.rule_id for violation in security.violations})
                original_security = check_sql(result.sql, dialect=self.dialect, schema=state["schema"])
                if not original_security.blocked:
                    logger.warning(
                        f"Reflector repair rejected for candidate {result.id}; preserving safe original SQL: {rule_ids}"
                    )
                    sql = (
                        original_security.normalized_sql if original_security.normalized_sql is not None else result.sql
                    )
                else:
                    logger.warning(
                        f"Reflector repair and original rejected for candidate {result.id}; "
                        f"retaining violations for revalidation: {rule_ids}"
                    )
                    blocked_repair = security
            elif security.normalized_sql is not None:
                sql = security.normalized_sql
        elif (
            self.single_statement_only
            and validate_single_read_only_sql(sql, dialect=self.dialect)
            and result.syntax_validated
        ):
            logger.warning(f"Reflector repair rejected for candidate {result.id}; preserving read-only original SQL")
            sql = result.sql
        result.sql, result.score, result.issues = sql, 0, []
        result.validation_passed = False
        result.syntax_validated = False
        result.security_checked = blocked_repair is not None
        result.security_violations = (
            [violation.to_dict() for violation in blocked_repair.violations] if blocked_repair else []
        )
        state["generation_results"].append(result)

    def _finish_repairs(self, state: BirdState) -> BirdState:
        state["validation_results"].clear()
        rendered = "\n".join(result.sql for result in state["generation_results"])
        message = f"=== Reflector ===\n{rendered}"
        logger.info(message)
        state["stream_message"] = message
        return state

    async def _fix_sql(self, val_res: list[Result]) -> list[str]:
        cases = [{"id": v.id, "sql": v.sql, "issues": v.issues} for v in val_res]
        expected_ids = [case["id"] for case in cases]
        cases = json.dumps(cases, ensure_ascii=False, separators=(",", ":"))
        context = {"cases": cases, "prompt": val_res[0].prompt}
        structured_kwargs = {
            "response_schema": REFLECTOR_RESULTS_SCHEMA,
            "response_schema_name": "bird_reflector",
            "json_validator": lambda value: validate_reflector_results(value, expected_ids),
        }
        response = await self.execute_with_llm_json(context, **structured_kwargs)
        return [quote_sql_placeholders(x["sql"]) for x in response]
