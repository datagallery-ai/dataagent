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

from dataagent.agents.bird.constants import DEFAULT_BIRD_PREVIEW_LIMIT
from dataagent.agents.bird.nodes.base_bird_node import BaseBirdNode
from dataagent.agents.bird.utils.bird_utils import sql_sha256, truncate
from dataagent.agents.bird.utils.sql_service import build_sql_service
from dataagent.agents.bird.workflow.state import BirdState, Result
from dataagent.core.errors import DataAgentError
from dataagent.utils.log import logger

_ExecutionOutput = tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]  # noqa: UP045


class ExecutorNode(BaseBirdNode):
    def __init__(self, **kwargs):
        super().__init__(name="executor", **kwargs)
        self.limit = kwargs.pop("limit", -1)
        self.preview_limit = kwargs.pop("preview_limit", DEFAULT_BIRD_PREVIEW_LIMIT)

    @staticmethod
    def _is_single(results: list) -> bool:
        """Return whether every successful candidate has the same non-empty row set."""
        if not results:
            return False
        normalized = set()
        for result in results:
            if result.error or not result.rows:
                return False
            normalized.add(frozenset(tuple(row) for row in result.rows))
        return len(normalized) == 1

    @staticmethod
    def _is_executable(result: Any) -> bool:
        return (
            result.validation_passed and result.error is None and result.columns is not None and result.rows is not None
        )

    @staticmethod
    def _has_warning(state: BirdState, phase: int, strategy: str, reason: str) -> bool:
        return any(
            warning.get("strategy") == strategy
            and warning.get("phase") == f"phase{phase}"
            and warning.get("reason") == reason
            for warning in state["generation_warnings"]
        )

    @staticmethod
    def _set_budget_warning(
        state: BirdState,
        phase: int,
        strategy: str,
        expected: int,
        actual: int,
    ) -> None:
        warning = {
            "strategy": strategy,
            "phase": f"phase{phase}",
            "expected": expected,
            "actual": actual,
            "reason": "budget_unmet",
        }
        retained_warnings = []
        for existing in state["generation_warnings"]:
            same_strategy = existing.get("strategy") == strategy
            same_phase = existing.get("phase") == f"phase{phase}"
            is_budget_warning = existing.get("reason") == "budget_unmet"
            if same_strategy and same_phase and is_budget_warning:
                continue
            retained_warnings.append(existing)
        state["generation_warnings"] = retained_warnings
        state["generation_warnings"].append(warning)
        logger.warning(f"Generator route warning: {warning}")

    def _record_execution_output(
        self,
        state: BirdState,
        result: Result,
        output: _ExecutionOutput,
    ) -> str:
        columns, rows, error = output
        result.columns, result.error = columns, error
        state["execution_results"].append(result)
        result.rows = None if rows is None else (rows[: self.limit] if self.limit >= 0 else rows)
        result.rows_preview = (
            None
            if rows is None
            else [
                tuple(truncate(value) for value in row)
                for row in (rows[: self.preview_limit] if self.preview_limit >= 0 else rows)
            ]
        )
        preview = str(result.rows_preview)
        if result.rows and result.rows_preview and len(result.rows) > len(result.rows_preview):
            preview += f" ... and {len(result.rows) - len(result.rows_preview)} more rows"
        return preview

    async def _aprocess(self, state: BirdState, runtime: Any = None) -> BirdState:
        _ = runtime
        config = self._get_agent_config("DATABASE.config", {}) or {}
        state["execution_results"] = []
        p = []
        validation_results = state["validation_results"]
        execution_candidates = [result for result in validation_results if result.validation_passed]
        outputs = await asyncio.to_thread(
            self._execute_queries, config, [result.sql for result in execution_candidates]
        )
        for result, output in zip(execution_candidates, outputs, strict=True):
            p.append(self._record_execution_output(state, result, output))
        state["validation_results"].clear()
        self._advance_generation_state(state)
        result_preview = "\n".join(p)
        message = f"=== Executor ===\n{result_preview}"
        safe_summaries = [
            (
                f"candidate_id={result.id} sql_sha256={sql_sha256(result.sql)} "
                f"row_count={len(result.rows) if result.rows is not None else 0} "
                f"error_code={'EXECUTION_ERROR' if result.error else 'NONE'}"
            )
            for result in state["execution_results"]
        ]
        logger.info("=== Executor ===\n{}", "\n".join(safe_summaries))
        state["stream_message"] = message
        return state

    def _advance_generation_state(self, state: BirdState) -> None:
        self._update_executable_budget(state)

    def _update_executable_budget(self, state: BirdState) -> None:
        current = list(state["execution_results"])
        accumulated = {result.id: result for result in state.get("executable_results", [])}
        if not state.get("budget_complete"):
            for result in current:
                if self._is_executable(result):
                    accumulated[result.id] = result
        state["executable_results"] = list(accumulated.values())
        state["needs_generation_retry"] = False
        state["needs_phase2"] = False

        if state.get("budget_complete"):
            state["execution_results"] = list(accumulated.values())
            return

        phase = state.get("generation_phase", 1)
        targets = state["generation_targets"]
        counted = [result for result in accumulated.values() if phase in (2, result.phase)]
        counts = dict.fromkeys(targets, 0)
        for result in counted:
            if result.strategy in counts:
                counts[result.strategy] += 1

        missing = {}
        for strategy, target in targets.items():
            if counts[strategy] < target:
                missing[strategy] = target - counts[strategy]
        max_attempts = state.get("generation_max_route_attempts", 0)
        retryable = [
            strategy
            for strategy in missing
            if state.get("generation_attempts", {}).get(f"{phase}:{strategy}", 0) < max_attempts
        ]
        if retryable:
            state["needs_generation_retry"] = True
            return

        for strategy in missing:
            if self._has_warning(state, phase, strategy, "no_few_shots"):
                continue
            self._set_budget_warning(state, phase, strategy, targets[strategy], counts[strategy])

        if phase == 1:
            phase1_results = [result for result in accumulated.values() if result.phase == 1]
            if self._is_single(phase1_results):
                state["budget_complete"] = True
                state["execution_results"] = phase1_results
            else:
                state["generation_phase"] = 2
                state["needs_phase2"] = True
            return

        if not accumulated:
            raise RuntimeError("Generator produced no SQL candidates.")
        state["budget_complete"] = True
        state["execution_results"] = list(accumulated.values())

    def _execute_queries(self, config: dict[str, Any], sqls: list[str]) -> list[_ExecutionOutput]:
        outputs: list[_ExecutionOutput] = []
        with build_sql_service(self.engine, config) as service:
            for sql in sqls:
                try:
                    outputs.append(service.execute(sql))
                except Exception as e:
                    outputs.append((None, None, _candidate_error_text(e)))
        return outputs


def _candidate_error_text(exc: Exception) -> str:
    """Keep going to Selector; surface a readable error instead of stopping the workflow."""
    if isinstance(exc, DataAgentError):
        return exc.fact
    return str(exc)
