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

from dataagent.agents.nl2sql.errors import SQLServiceError
from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import flatten_schema, metadata_parser
from dataagent.agents.nl2sql.utils.sql_guard import SQLGuardError, guard_sql, resolve_sqlglot_dialect
from dataagent.agents.nl2sql.utils.sql_service import build_sql_service
from dataagent.agents.nl2sql.workflow.state import NL2SQLState, Result
from dataagent.utils.log import logger


class ValidatorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(name="validator", **kwargs)
        self.db_explain = kwargs.pop("db_explain", False)
        self.keyword_match = kwargs.pop("keyword_match", False)
        self.metadata_match = kwargs.pop("metadata_match", False)
        self.read_only = kwargs.pop("read_only", True)

    def _process(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        semantic_res = self._validate_semantic(state)
        syntax_res = self._validate_syntax(state["generation_results"])
        if self.metadata_match:
            metadata_res = self._validate_metadata(state["schema"], state["generation_results"])
        else:
            metadata_res = [{"score": 1, "issues": []}] * len(state["generation_results"])
        state["validation_results"] = self._combine_validation_results(
            state["generation_results"], semantic_res, syntax_res, metadata_res
        )
        state["generation_results"].clear()
        trace_id = state.get("trace_id") or ""
        p = "\n".join(
            [
                f"Score: {v.score:.2f}, sql_sha256={v.sql_sha256}, Issues: {v.issues}"
                for v in state["validation_results"]
            ]
        )
        message = f"=== Validator ===\ntrace_id={trace_id}\n{p}"
        logger.info(message)
        state["stream_message"] = message
        return state

    def _validate_semantic(self, state: NL2SQLState) -> list[dict[str, Any]]:
        res = [{"id": r.id, "sql": r.sql} for r in state["generation_results"]]
        context = {
            "schema": state["schema_str"],
            "evidence": state["evidence"],
            "question": state["question"],
            "sql_rules": state["sql_rules"],
            "sqls": json.dumps(res),
        }
        for _ in range(3):
            out = self.execute_with_llm_json(context, "validate_semantic_")
            if len(out) == len(state["generation_results"]):
                res = out
                for r in res:
                    del r["id"]
                break
        else:
            # skip if fail
            logger.warning("Semantic validator failed.")
            res = [{"score": 1, "issues": []}] * len(state["generation_results"])
        return res

    def _validate_syntax(self, gen_res: list[Result]) -> list[dict[str, Any]]:
        res = []
        for gr in gen_res:
            issues = self._validate_with_sqlglot(gr.sql)
            if self.keyword_match:
                issues += self._validate_with_keyword_match(gr.sql)
            # Never EXPLAIN statements that already failed the hard SQL gate.
            if self.db_explain and not issues:
                issues += self._validate_with_db_explain(gr.sql)
            res.append({"score": 0 if issues else 1, "issues": issues})
        return res

    def _validate_with_keyword_match(self, sql: str) -> list[str]:
        issues = []
        s = sql.lower()
        s = re.sub(r"'.*?'", "''", s)
        s = re.sub(r'".*?"', '""', s)
        patterns = [
            (r"\bwith\b", "`WITH` is not allowed. Use nested `SELECT` instead."),
            (r"\bdistinct\b", "`DISTINCT` is not allowed. Use `GROUP BY` instead."),
        ]
        for pattern, msg in patterns:
            if re.search(pattern, s):
                issues.append(msg)
        return issues

    def _validate_with_db_explain(self, sql: str) -> list[str]:
        config = self._get_agent_config("DATABASE.config", {}) or {}
        try:
            with build_sql_service(self.sql_service_engine, config) as explain_service:
                res = explain_service.explain(sql)
            return [res] if res else []
        except SQLServiceError as e:
            return [str(e.detail or e.message or e)]
        except Exception as e:
            return [str(e)]

    def _validate_with_sqlglot(self, sql: str) -> list[str]:
        try:
            guard_sql(sql, dialect=resolve_sqlglot_dialect(self.engine), read_only=self.read_only)
            return []
        except SQLGuardError as e:
            return [str(e)]
        except Exception as e:
            return [str(e)]

    def _validate_metadata(self, schema: dict, gen_res: list[Result]) -> list[dict[str, Any]]:
        res = []
        valid = flatten_schema(schema)
        context = {"sqls": json.dumps([gr.sql for gr in gen_res])}
        raws = metadata_parser(self.execute_with_llm(context, "extract_columns_"))
        for raw in raws:
            invalid_cols = raw.keys() - valid
            issues = [f"Invalid columns: {', '.join(map(repr, invalid_cols))}"] if invalid_cols else []
            res.append({"score": 0 if issues else 1, "issues": issues})
        return res

    def _combine_validation_results(
        self,
        gen_res: list[Result],
        semantic_res: list[dict[str, Any]],
        syntax_res: list[dict[str, Any]],
        metadata_res: list[dict[str, Any]],
    ) -> list[Result]:
        result = []
        for gs, sm_res, sn_res, md_res in zip(gen_res, semantic_res, syntax_res, metadata_res, strict=True):
            score, all_issues = 1, []
            for res in [sm_res, sn_res, md_res]:
                score *= res["score"]
                all_issues.extend(res["issues"])
            gs.score, gs.issues = score, all_issues
            result.append(gs)
        return result
