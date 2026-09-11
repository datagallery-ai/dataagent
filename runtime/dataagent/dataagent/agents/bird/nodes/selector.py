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
import math
from typing import Any

from dataagent.agents.bird.nodes.base_bird_node import BaseBirdNode
from dataagent.agents.bird.utils.bird_utils import sql_sha256
from dataagent.agents.bird.utils.structured_json import (
    SCORE_RESULTS_SCHEMA,
    load_score_results_json,
    validate_score_results,
)
from dataagent.agents.bird.workflow.state import BirdState, Result
from dataagent.core.errors import DataAgentError
from dataagent.utils.log import logger


class SelectorNode(BaseBirdNode):
    def __init__(self, **kwargs):
        super().__init__(name="selector", **kwargs)
        self.response_format_type = str(self.config.get("response_format_type", "json_schema"))
        self.review_consistency_threshold = float(self.config.get("review_consistency_threshold", 0.5))
        if not math.isfinite(self.review_consistency_threshold) or not 0 <= self.review_consistency_threshold <= 1:
            raise ValueError("review_consistency_threshold must be a finite number between 0 and 1")

    @staticmethod
    def _result_groups(res: list[Result]) -> list[dict[str, Any]]:
        successful = []
        for result in res:
            if not result.error and result.columns is not None and result.rows is not None:
                successful.append(result)
        non_empty = [result for result in successful if result.rows]
        candidates = non_empty or successful
        result_map: dict[frozenset, dict[str, Any]] = {}
        for result in candidates:
            key = frozenset(tuple(row) for row in result.rows or [])
            if key not in result_map:
                result_map[key] = {"vote": 1, "result": result}
                continue
            group = result_map[key]
            group["vote"] += 1
            if len(result.sql) < len(group["result"].sql):
                group["result"] = result
        return list(result_map.values())

    async def _aprocess(self, state: BirdState, runtime: Any = None) -> BirdState:
        _ = runtime
        best, summary, vote_count = await self._choose_result(state)
        state["stream_message"] = f"=== Selector ===\n{summary}"
        self._log_candidates(state, vote_count)
        return self._finalize_result(state, best)

    async def _choose_result(self, state: BirdState) -> tuple[Result, str, int | None]:
        groups = self._result_groups(state["execution_results"])
        if not groups:
            raise DataAgentError(
                source="internal",
                component="bird",
                fact="No successfully executed BIRD SQL candidate is available for selection.",
            )
        valid_count = sum(group["vote"] for group in groups)
        baseline = max(groups, key=lambda group: group["vote"])
        ambiguous = len(groups) > 1
        if ambiguous:
            ranked_votes = sorted((group["vote"] for group in groups), reverse=True)
            top_share = ranked_votes[0] / valid_count
            ambiguous = ranked_votes[0] == ranked_votes[1] or top_share < self.review_consistency_threshold
        if ambiguous:
            best = await self._review_groups(state, groups, valid_count)
            if best:
                summary = f"Ambiguity review selected: {best.sql}"
            else:
                best = baseline["result"]
                best.confidence = baseline["vote"] / valid_count
                summary = f"Ambiguity review fallback with {baseline['vote']} votes: {best.sql}"
        else:
            best = baseline["result"]
            best.confidence = baseline["vote"] / valid_count
            summary = f"Result-set vote selected with {baseline['vote']} votes: {best.sql}"
        return best, summary, baseline["vote"]

    def _log_candidates(self, state: BirdState, shortcut_vote_count: int | None) -> None:
        candidate_summaries = [
            (
                f"candidate_id={e.id} sql_sha256={sql_sha256(e.sql)} "
                f"row_count={len(e.rows) if e.rows is not None else 0} "
                f"confidence={e.confidence:.2f} "
                f"error_code={'EXECUTION_ERROR' if e.error else 'NONE'}"
            )
            for e in state["execution_results"]
        ]
        if shortcut_vote_count is not None:
            candidate_summaries.append(f"shortcut_vote_count={shortcut_vote_count}")
        logger.info("=== Selector ===\n{}", "\n".join(candidate_summaries))

    def _finalize_result(self, state: BirdState, best: Result) -> BirdState:
        state["sql"], state["confidence"] = best.sql, best.confidence
        state["columns"], state["rows"], state["rows_preview"] = best.columns, best.rows, best.rows_preview
        state["error"] = best.error
        rendered = f"{state['sql']}\n{state['rows_preview']}"
        if best.rows and best.rows_preview is not None and len(best.rows) > len(best.rows_preview):
            rendered += f" ... and {len(best.rows) - len(best.rows_preview)} more rows"
        logger.info(
            "=== Final Result ===\nsql_sha256={} row_count={} confidence={:.2f} error_code={}",
            sql_sha256(best.sql),
            len(best.rows) if best.rows is not None else 0,
            best.confidence,
            "EXECUTION_ERROR" if best.error else "NONE",
        )
        state["stream_message"] = f"=== Final Result ===\n{rendered}"
        return state

    async def _review_groups(
        self,
        state: BirdState,
        groups: list[dict[str, Any]],
        valid_count: int,
    ) -> Result | None:
        review_results = []
        for group in groups:
            result = group["result"]
            review_results.append(
                {
                    "id": result.id,
                    "sql": result.sql,
                    "cols": result.columns,
                    "rows": result.rows_preview,
                    "err": result.error,
                    "votes": group["vote"],
                    "share": group["vote"] / valid_count,
                }
            )
        expected_ids = [item["id"] for item in review_results]
        context = {
            "schema": state["schema_str"],
            "question": state["question"],
            "sql_rules": state["sql_rules"],
            "res": json.dumps(review_results, default=str),
        }
        try:
            structured_kwargs = {
                "response_schema": SCORE_RESULTS_SCHEMA,
                "response_schema_name": "bird_selector",
                "response_format_type": self.response_format_type,
                "json_loader": load_score_results_json,
                "json_validator": lambda value: validate_score_results(value, expected_ids),
            }
            selections = await self.execute_with_llm_json(context, "ambiguity_", **structured_kwargs)
            max_score = max(item["score"] for item in selections)
            winners = [item for item in selections if item["score"] == max_score]
            if len(winners) != 1:
                raise ValueError("selector review did not produce a unique winner")
            by_id = {group["result"].id: group["result"] for group in groups}
            for selection in selections:
                result = by_id[selection["id"]]
                result.confidence = selection["score"]
                result.issues = result.issues + selection["issues"]
            return by_id[winners[0]["id"]]
        except Exception as exc:
            logger.warning(f"Selector ambiguity review failed; using vote baseline: {exc}")
            return None
