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

from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import sql_parser
from dataagent.agents.nl2sql.workflow.state import NL2SQLState, Result
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PromptTemplate
from dataagent.utils.constants import DEFAULT_NL2SQL_NUM_SAMPLES, DEFAULT_NL2SQL_NUM_WORKERS, NL2SQL_PROMPT_PREFIX
from dataagent.utils.log import logger


class GeneratorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs):
        super().__init__(name="generator", **kwargs)
        self.num_workers = kwargs.pop("num_workers", DEFAULT_NL2SQL_NUM_WORKERS)
        self.num_samples = kwargs.pop("num_samples", DEFAULT_NL2SQL_NUM_SAMPLES)
        self.strategies = kwargs.pop("strategies", ["prompt"])

    async def generate_with_llm(self, strategy: str, settings: dict, context: dict):
        """Generate SQL candidates for one strategy with the configured LLM."""
        system_prompt = PromptTemplate.from_package_relative(
            f"{NL2SQL_PROMPT_PREFIX}/generator/{strategy}_system"
        ).apply_prompt_template(**settings)
        user_prompt = PromptTemplate.from_package_relative(
            f"{NL2SQL_PROMPT_PREFIX}/generator/{strategy}_user"
        ).apply_prompt_template(**context)
        prompts = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
        content = (await llm_manager.get_default_llm().ainvoke(prompts)).content
        self._dump_llm_context(system_prompt, user_prompt, content, self.name, strategy)
        expected_num_sql = settings.get("num_samples", 1) if strategy == "prompt" else 1
        sqls = sql_parser(content)[-expected_num_sql:]
        prompt_history = system_prompt + "\n\n" + user_prompt
        return [(sql, prompt_history, strategy) for sql in sqls]

    async def strategy_prompt(self, settings, context):
        """Generate prompt-strategy candidates."""
        settings["num_samples"] = self.num_samples
        return await self.generate_with_llm("prompt", settings, context)

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

    async def _aprocess(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        _ = runtime
        settings = {"dialect": self.dialect}
        context = {
            "question": state["question"],
            "schema": state["schema_str"],
            "sql_rules": state["sql_rules"],
            "evidence": state["evidence"],
            "few_shot_examples": state["few_shot_examples"],
        }
        results = []
        if self.num_workers * len(self.strategies) <= 0:
            raise ValueError("max_workers must be greater than 0")

        async def _run_named(strategy: str):
            try:
                return strategy, await self.run_strategy(strategy, settings.copy(), context), None
            except Exception as exc:
                return strategy, None, exc

        tasks = [
            asyncio.create_task(_run_named(strategy)) for strategy in self.strategies for _ in range(self.num_workers)
        ]
        failures: list[tuple[str, Exception]] = []
        try:
            for task in asyncio.as_completed(tasks):
                strategy, res, exc = await task
                if exc is not None:
                    failures.append((strategy, exc))
                    logger.warning(f"Generator strategy {strategy} failed: {exc}")
                else:
                    results.extend(res)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if not results:
            from dataagent.core.errors import DataAgentError

            last_strategy, last_exc = failures[-1] if failures else ("", None)
            if isinstance(last_exc, DataAgentError):
                fact = last_exc.fact
                if last_strategy and last_strategy not in fact:
                    fact = f"{fact}；strategy={last_strategy}"
                raise DataAgentError(
                    source=last_exc.source,
                    component=last_exc.component,
                    fact=fact,
                    trace_id=last_exc.trace_id,
                ) from last_exc
            inner = DataAgentError.from_exception(last_exc, component="nl2sql") if last_exc is not None else None
            fact = inner.fact if inner is not None else "模型调用失败"
            if last_strategy:
                fact = f"{fact}；strategy={last_strategy}"
            raise DataAgentError(
                fact=fact,
                source="llm",
                component="nl2sql",
            ) from last_exc
        for i, (sql, prompt, strategy) in enumerate(results):
            state["generation_results"].append(Result(id=i, sql=sql, prompt=prompt, strategy=strategy))
        state["sql"] = state["generation_results"][0].sql
        p = "\n".join([f"[{s.strategy}]\n{s.sql}" for s in state["generation_results"]])
        message = f"=== Generator ===\n{p}"
        logger.info(message)
        state["stream_message"] = message
        return state
