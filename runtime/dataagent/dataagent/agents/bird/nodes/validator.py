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
import json
import re
from typing import Any

from dataagent.agents.bird.errors import LLMOutputParseError
from dataagent.agents.bird.nodes.base_bird_node import BaseBirdNode
from dataagent.agents.bird.security import check_sql
from dataagent.agents.bird.utils.sql_service import build_sql_service
from dataagent.agents.bird.utils.sql_validation import validate_single_read_only_sql
from dataagent.agents.bird.utils.structured_json import (
    SCORE_RESULTS_SCHEMA,
    load_score_results_json,
    validate_score_results,
)
from dataagent.agents.bird.workflow.state import BirdState, Result
from dataagent.core.errors import DataAgentError
from dataagent.utils.log import logger


class ValidatorNode(BaseBirdNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(name="validator", **kwargs)
        self.sql_security_enabled = bool(self.config.get("sql_security_enabled", False))
        self.response_format_type = str(kwargs.pop("response_format_type", "json_schema"))
        self.semantic_failure_score = float(kwargs.pop("semantic_failure_score", 1.0))
        if self.semantic_failure_score not in (0.0, 1.0):
            raise ValueError("semantic_failure_score must be 0 or 1")

    @staticmethod
    def _validate_branch_compliance(sql: str) -> list[str]:
        """Map non-duplicated BIRD checker rules to Validator issues without rewriting SQL."""
        issues = []
        normalized = re.sub(r"\s+", " ", sql).strip()
        lowered = normalized.lower()
        join_on = re.findall(
            r"\bjoin\b.*?\bon\b(.*?)(?=\bjoin\b|\bwhere\b|\bgroup\b|\border\b|\blimit\b|$)",
            lowered,
        )
        if any(re.search(r"\bor\b|\bin\s*\(", clause) for clause in join_on):
            issues.append("BIRD_JOIN_ON_MULTIPLE: JOIN ON must use one explicit column equality, not OR/IN.")
        if re.search(r"\b[a-z_][\w]*\.\*", lowered):
            issues.append("BIRD_QUALIFIED_STAR: select concrete columns instead of a qualified star.")
        select_clause = re.split(r"\bfrom\b", lowered, maxsplit=1)[0]
        if "||" in select_clause:
            issues.append("BIRD_SELECT_CONCAT: return requested columns separately instead of concatenating them.")
        # The branch's MAX/MIN rewrites are intentionally not mapped here: without
        # question semantics they cannot be distinguished from valid grouped aggregates.
        if re.search(r"strftime\s*\([^)]*\)\s*(?:=|<>|!=|<=|>=|<|>)\s*\d{2,}\b", lowered):
            issues.append("BIRD_STRFTIME_LITERAL: compare STRFTIME output with a quoted text literal.")
        return issues

    async def _aprocess(self, state: BirdState, runtime: Any = None) -> BirdState:
        _ = runtime
        semantic_res = await self._validate_semantic(state)
        syntax_res = await self._validate_syntax(state["generation_results"], state["schema"])
        state["validation_results"] = self._combine_validation_results(
            state["generation_results"], semantic_res, syntax_res
        )
        state["generation_results"].clear()
        p = "\n".join([f"Score: {v.score:.2f}, Issues: {v.issues}" for v in state["validation_results"]])
        message = f"=== Validator ===\n{p}"
        logger.info(message)
        state["stream_message"] = message
        return state

    async def _validate_semantic(self, state: BirdState) -> list[dict[str, Any]]:
        res = [{"id": r.id, "sql": r.sql} for r in state["generation_results"]]
        expected_ids = [r["id"] for r in res]
        context = {
            "schema": state["schema_str"],
            "evidence": state["evidence"],
            "question": state["question"],
            "sql_rules": state["sql_rules"],
            "sqls": json.dumps(res),
        }
        structured_kwargs = {
            "response_schema": SCORE_RESULTS_SCHEMA,
            "response_schema_name": "bird_validator",
            "response_format_type": self.response_format_type,
            "json_loader": load_score_results_json,
            "json_validator": lambda value: validate_score_results(value, expected_ids),
        }
        try:
            res = await self.execute_with_llm_json(context, "validate_semantic_", **structured_kwargs)
        except (LLMOutputParseError, DataAgentError) as exc:
            if isinstance(exc, DataAgentError) and not isinstance(exc.__cause__, LLMOutputParseError):
                raise
            logger.warning("Semantic validator failed.")
            return [
                {"score": self.semantic_failure_score, "issues": ["Semantic validator output was invalid."]}
                for _ in state["generation_results"]
            ]
        for result in res:
            del result["id"]
        return res

    async def _validate_syntax(self, gen_res: list[Result], schema: dict) -> list[dict[str, Any]]:
        res = []
        for gr in gen_res:
            issues = validate_single_read_only_sql(gr.sql, dialect=self.dialect, read_only=True)
            gr.syntax_validated = not issues
            if self.dialect == "sqlite":
                issues += self._validate_branch_compliance(gr.sql)
            if self.sql_security_enabled:
                security = check_sql(gr.sql, dialect=self.dialect, schema=schema)
                if not security.blocked and security.normalized_sql is not None:
                    gr.sql = security.normalized_sql
                gr.security_checked = True
                gr.security_violations = [violation.to_dict() for violation in security.violations]
                issues.extend(f"{violation.rule_id}: {violation.message}" for violation in security.violations)
            else:
                gr.security_checked = False
                gr.security_violations = []
            if not issues:
                issues += await self._validate_with_db_explain(gr.sql)
            res.append({"score": 0 if issues else 1, "issues": issues})
        return res

    async def _validate_with_db_explain(self, sql: str) -> list[str]:
        return await asyncio.to_thread(self._validate_with_db_explain_sync, sql)

    def _validate_with_db_explain_sync(self, sql: str) -> list[str]:
        config = self._get_agent_config("DATABASE.config", {}) or {}
        try:
            with build_sql_service(self.engine, config) as explain_service:
                res = explain_service.explain(sql)
            return [res] if res else []
        except Exception as e:
            return [str(e)]

    def _combine_validation_results(
        self,
        gen_res: list[Result],
        semantic_res: list[dict[str, Any]],
        syntax_res: list[dict[str, Any]],
    ) -> list[Result]:
        result = []
        for gs, sm_res, sn_res in zip(gen_res, semantic_res, syntax_res, strict=True):
            score, all_issues = 1, []
            for res in [sm_res, sn_res]:
                score *= res["score"]
                all_issues.extend(res["issues"])
            gs.score, gs.issues = score, all_issues
            result.append(gs)
        return result
