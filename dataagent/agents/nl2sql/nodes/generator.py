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
from dataagent.agents.nl2sql.utils.nl2sql_utils import process_dimension_joins, sql_parser
from dataagent.agents.nl2sql.workflow.state import NL2SQLState
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
        tasks = [
            asyncio.create_task(self.run_strategy(strategy, settings.copy(), context))
            for strategy in self.strategies
            for _ in range(self.num_workers)
        ]
        failures: list[Exception] = []
        try:
            for task in asyncio.as_completed(tasks):
                try:
                    res = await task
                except Exception as exc:
                    failures.append(exc)
                    logger.warning(f"Generator strategy task failed: {exc}")
                else:
                    results.extend(res)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if not results:
            if len(failures) == len(tasks):
                raise failures[0]
            error = RuntimeError("Generator produced no SQL candidates.")
            if failures:
                raise error from failures[0]
            raise error
        state["generation_results"].extend(
            await process_dimension_joins(
                results,
                state,
                scenario=self._get_agent_config("DATABASE.perceptor_type", ""),
                dialect=self.dialect,
                execute_with_llm=self.execute_with_llm,
            )
        )
        state["sql"] = state["generation_results"][0].sql
        p = "\n".join([f"[{s.strategy}]\n{s.sql}" for s in state["generation_results"]])
        message = f"=== Generator ===\n{p}"
        logger.info(message)
        state["stream_message"] = message
        return state
