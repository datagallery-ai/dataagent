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

from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient
from dataagent.agents.bird.constants import BIRD_PROMPT_PREFIX, DEFAULT_BIRD_NUM_WORKERS, DEFAULT_BIRD_REF_RETRIES
from dataagent.agents.bird.errors import SchemaNotFoundError
from dataagent.agents.bird.nodes.base_bird_node import BaseBirdNode
from dataagent.agents.bird.utils.bird_utils import sql_parser
from dataagent.agents.bird.workflow.state import BirdState, Result
from dataagent.utils.log import logger


class GeneratorNode(BaseBirdNode):
    STRATEGIES = ("dc", "skeleton", "icl")

    def __init__(self, **kwargs):
        super().__init__(name="generator", **kwargs)
        self.num_workers = kwargs.pop("num_workers", DEFAULT_BIRD_NUM_WORKERS)
        self.phase1_budget = kwargs.pop("phase1_budget", 1)
        self.max_route_attempts = kwargs.pop("max_route_attempts", 3)
        self.icl_top_k = kwargs.pop("icl_top_k", 3)
        self.max_tokens = kwargs.pop("max_tokens", None)
        if self.max_tokens is not None and (
            isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int) or self.max_tokens <= 0
        ):
            raise ValueError("Generator max_tokens must be a positive integer or null.")

    @staticmethod
    def _add_warning(warnings: list[dict[str, Any]], **warning: Any) -> None:
        if warning not in warnings:
            warnings.append(warning)
        logger.warning(f"Generator route warning: {warning}")

    @staticmethod
    def _is_single(results: list[Result]) -> bool:
        if not results:
            return False
        normalized = set()
        for result in results:
            if result.error or not result.rows:
                return False
            normalized.add(frozenset(tuple(row) for row in result.rows))
        return len(normalized) == 1

    @staticmethod
    def _format_few_shots(payload: Any) -> str:
        if not isinstance(payload, list):
            return ""
        rendered = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            value = (
                next(iter(item.values())) if len(item) == 1 and isinstance(next(iter(item.values())), dict) else item
            )
            question = next(
                (value.get(key) for key in ("query", "question", "natural_language_query") if value.get(key)),
                None,
            )
            sql = next(
                (value.get(key) for key in ("expression", "sql", "gold_sql", "sql_text") if value.get(key)),
                None,
            )
            if question and sql:
                rendered.append(f"Question: {question}\n```sql\n{sql}\n```")
        return "\n\n".join(rendered)

    async def generate_with_llm(self, strategy: str, settings: dict, context: dict):
        """Generate SQL candidates for one strategy with the configured LLM."""
        system_prompt = self._get_prompt_template(
            f"{BIRD_PROMPT_PREFIX}/generator/{strategy}_system"
        ).apply_prompt_template(**settings)
        user_prompt = self._get_prompt_template(
            f"{BIRD_PROMPT_PREFIX}/generator/{strategy}_user"
        ).apply_prompt_template(**context)
        prompts = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
        invoke_kwargs = {"max_tokens": self.max_tokens} if self.max_tokens is not None else {}
        content = (await self._invoke_llm(prompts, **invoke_kwargs)).content
        self._dump_llm_context(system_prompt, user_prompt, content, self.name, strategy)
        sqls = sql_parser(content)[-1:]
        prompt_history = system_prompt + "\n\n" + user_prompt
        return [(sql, prompt_history, strategy) for sql in sqls]

    async def strategy_skeleton(self, settings, context):
        """Generate skeleton-strategy candidates."""
        return await self.generate_with_llm("skeleton", settings, context)

    async def strategy_icl(self, settings, context):
        """Generate in-context-learning candidates."""
        return await self.generate_with_llm("icl", settings, context)

    async def strategy_dc(self, settings, context):
        """Generate divide-and-conquer candidates."""
        return await self.generate_with_llm("dc", settings, context)

    async def run_strategy(self, strategy, settings, context):
        """Run one configured generation strategy."""
        fn = getattr(self, f"strategy_{strategy}", None)
        if fn is None:
            raise ValueError(f"Unknown strategy: {strategy}")
        return await fn(settings, context)

    async def _aprocess(self, state: BirdState, runtime: Any = None) -> BirdState:
        _ = runtime
        if state.get("schema_linking_error"):
            raise SchemaNotFoundError(detail=state["schema_linking_error"])
        self._initialize_two_phase_state(state)
        settings = {"dialect": self.dialect}
        context = {
            "question": state["question"],
            "schema": state["schema_str"],
            "sql_rules": state["sql_rules"],
            "evidence": state["evidence"],
            "few_shot_examples": state["few_shot_examples"],
        }
        context["few_shot_examples"] = await self._load_icl_examples(state)
        return await self._aprocess_two_phase(state, settings, context)

    def _initialize_two_phase_state(self, state: BirdState) -> None:
        """Initialize two-phase bookkeeping only when that workflow is active."""
        state.setdefault("generation_phase", 1)
        state.setdefault("generation_warnings", [])
        state.setdefault("executable_results", [])
        state.setdefault("generation_targets", {})
        state.setdefault("generation_attempts", {})
        state.setdefault("generation_slots", {})
        state.setdefault("generation_max_route_attempts", 0)
        state.setdefault("needs_generation_retry", False)
        state.setdefault("budget_complete", False)
        state.setdefault("needs_phase2", False)

    async def _aprocess_two_phase(self, state: BirdState, settings: dict, context: dict) -> BirdState:
        phase = state.get("generation_phase", 1)
        total_budgets = dict.fromkeys(self.STRATEGIES, self.num_workers)
        if phase == 1:
            targets = {strategy: min(self.phase1_budget, total) for strategy, total in total_budgets.items()}
            accepted = [result for result in state.get("executable_results", []) if result.phase == 1]
        else:
            targets = total_budgets
            accepted = list(state.get("executable_results", []))
        accepted_counts = dict.fromkeys(self.STRATEGIES, 0)
        for result in accepted:
            if result.strategy in accepted_counts:
                accepted_counts[result.strategy] += 1
        budgets = {strategy: max(0, targets[strategy] - accepted_counts[strategy]) for strategy in self.STRATEGIES}
        state["generation_targets"] = targets
        state["generation_max_route_attempts"] = self.max_route_attempts
        state["needs_generation_retry"] = False
        state["needs_phase2"] = False

        results = await self._generate_phase(
            phase=phase,
            budgets=budgets,
            settings=settings,
            context=context,
            warnings=state["generation_warnings"],
            state=state,
            targets=targets,
        )
        state["generation_results"] = results
        if not results:
            if phase == 1:
                self._finish_exhausted_phase(state, phase, targets)
                if state.get("needs_phase2"):
                    return await self._aprocess_two_phase(state, settings, context)
                state["stream_message"] = "=== Generator Phase 1 ===\nNo additional SQL candidates."
                return state
            executable = list(state.get("executable_results", []))
            if executable:
                self._finish_exhausted_phase(state, phase, targets)
                state["stream_message"] = f"=== Generator Phase {phase} ===\nNo additional SQL candidates."
                return state
            raise RuntimeError("Generator produced no SQL candidates.")

        state["ref_retries"] = self._get_agent_config("CORE.reflector.ref_retries", DEFAULT_BIRD_REF_RETRIES)
        state["proceed"] = True
        state["budget_complete"] = False
        state["sql"] = results[0].sql
        rendered = "\n".join(f"[{result.strategy}/phase{phase}]\n{result.sql}" for result in results)
        message = f"=== Generator Phase {phase} ===\n{rendered}"
        logger.info(message)
        state["stream_message"] = message
        return state

    async def _generate_phase(
        self,
        *,
        phase: int,
        budgets: dict[str, int],
        settings: dict,
        context: dict,
        warnings: list[dict[str, Any]],
        state: BirdState,
        targets: dict[str, int],
    ) -> list[Result]:
        produced: dict[str, list[tuple[int, str, str]]] = {strategy: [] for strategy in self.STRATEGIES}
        next_slots = state["generation_slots"]
        attempts = state["generation_attempts"]
        failures: list[Exception] = []

        while True:
            tasks = self._schedule_phase_tasks(
                phase=phase,
                budgets=budgets,
                settings=settings,
                context=context,
                warnings=warnings,
                state=state,
                targets=targets,
                produced=produced,
                next_slots=next_slots,
                attempts=attempts,
            )
            if not tasks:
                break
            await self._collect_phase_tasks(tasks, produced, budgets, phase, failures)
            if all(
                len(produced[strategy]) >= budget or attempts.get(f"{phase}:{strategy}", 0) >= self.max_route_attempts
                for strategy, budget in budgets.items()
            ):
                break

        results = self._build_phase_results(
            phase=phase,
            budgets=budgets,
            state=state,
            produced=produced,
        )
        if not results and failures:
            logger.warning(f"All Generator phase {phase} attempts failed; first error: {failures[0]}")
        return results

    def _schedule_phase_tasks(
        self,
        *,
        phase: int,
        budgets: dict[str, int],
        settings: dict,
        context: dict,
        warnings: list[dict[str, Any]],
        state: BirdState,
        targets: dict[str, int],
        produced: dict[str, list[tuple[int, str, str]]],
        next_slots: dict,
        attempts: dict,
    ) -> list[asyncio.Task]:
        tasks = []
        icl_available = bool(context.get("few_shot_examples", "").strip())
        for strategy in self.STRATEGIES:
            missing = budgets[strategy] - len(produced[strategy])
            attempt_key = f"{phase}:{strategy}"
            if missing <= 0 or attempts.get(attempt_key, 0) >= self.max_route_attempts:
                continue
            if strategy == "icl" and not icl_available:
                attempts[attempt_key] = self.max_route_attempts
                actual = sum(
                    1
                    for result in state.get("executable_results", [])
                    if result.strategy == strategy and phase in (2, result.phase)
                )
                self._add_warning(
                    warnings,
                    strategy=strategy,
                    phase=f"phase{phase}",
                    expected=targets[strategy],
                    actual=actual,
                    reason="no_few_shots",
                )
                continue
            attempts[attempt_key] = attempts.get(attempt_key, 0) + 1
            for _ in range(missing):
                slot_key = f"{phase}:{strategy}"
                slot = next_slots.get(slot_key, 0)
                next_slots[slot_key] = slot + 1
                task_settings = settings.copy()
                tasks.append(
                    asyncio.create_task(self._run_tagged_strategy(strategy, slot, task_settings, context.copy()))
                )
        return tasks

    async def _collect_phase_tasks(
        self,
        tasks: list[asyncio.Task],
        produced: dict[str, list[tuple[int, str, str]]],
        budgets: dict[str, int],
        phase: int,
        failures: list[Exception],
    ) -> None:
        try:
            for task in asyncio.as_completed(tasks):
                try:
                    strategy, slot, candidates = await task
                except Exception as exc:
                    failures.append(exc)
                    logger.warning(f"Generator phase {phase} strategy task failed: {exc}")
                    continue
                if candidates and len(produced[strategy]) < budgets[strategy]:
                    sql, prompt, _ = candidates[0]
                    produced[strategy].append((slot, sql, prompt))
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def _build_phase_results(
        self,
        *,
        phase: int,
        budgets: dict[str, int],
        state: BirdState,
        produced: dict[str, list[tuple[int, str, str]]],
    ) -> list[Result]:
        results: list[Result] = []
        for strategy_index, strategy in enumerate(self.STRATEGIES):
            candidates = sorted(produced[strategy], key=lambda item: item[0])[: budgets[strategy]]
            for slot, sql, prompt in candidates:
                result_id = phase * 1_000_000 + strategy_index * 10_000 + slot
                results.append(Result(id=result_id, sql=sql, prompt=prompt, strategy=strategy, phase=phase))
        return results

    def _finish_exhausted_phase(self, state: BirdState, phase: int, targets: dict[str, int]) -> None:
        executable = list(state.get("executable_results", []))
        for strategy, expected in targets.items():
            actual = sum(1 for result in executable if result.strategy == strategy and phase in (2, result.phase))
            if actual < expected and not any(
                warning.get("strategy") == strategy
                and warning.get("phase") == f"phase{phase}"
                and warning.get("reason") == "no_few_shots"
                for warning in state["generation_warnings"]
            ):
                self._add_warning(
                    state["generation_warnings"],
                    strategy=strategy,
                    phase=f"phase{phase}",
                    expected=expected,
                    actual=actual,
                    reason="budget_unmet",
                )
        state["budget_complete"] = True
        state["needs_generation_retry"] = False
        if phase == 1:
            phase1_results = [result for result in executable if result.phase == 1]
            if self._is_single(phase1_results):
                state["execution_results"] = phase1_results
            else:
                state["generation_phase"] = 2
                state["needs_phase2"] = True
                state["budget_complete"] = False
        else:
            state["execution_results"] = executable

    async def _run_tagged_strategy(self, strategy: str, slot: int, settings: dict, context: dict):
        return strategy, slot, await self.run_strategy(strategy, settings, context)

    async def _load_icl_examples(self, state: BirdState) -> str:
        cached = state.get("few_shot_examples", "")
        if state.get("few_shot_lookup_complete"):
            return cached
        state["few_shot_lookup_complete"] = True
        try:
            client = SemanticServiceClient.from_config(self._config_manager)
            payload = await asyncio.to_thread(client.sql_few_shots, state["question"], self.icl_top_k)
        except Exception as exc:
            logger.warning(f"ICL semantic-service retrieval failed: {exc}")
            return ""
        examples = self._format_few_shots(payload)
        state["few_shot_examples"] = examples
        return examples
