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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import sql_parser
from dataagent.agents.nl2sql.workflow.state import NL2SQLState, Result
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PromptTemplate
from dataagent.core.utils.performance import submit_in_perf_context
from dataagent.utils.constants import DEFAULT_NL2SQL_NUM_SAMPLES, DEFAULT_NL2SQL_NUM_WORKERS, NL2SQL_PROMPT_PREFIX
from dataagent.utils.log import logger


class GeneratorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs):
        super().__init__(name="generator", **kwargs)
        self.num_workers = kwargs.pop("num_workers", DEFAULT_NL2SQL_NUM_WORKERS)
        self.num_samples = kwargs.pop("num_samples", DEFAULT_NL2SQL_NUM_SAMPLES)
        self.strategies = kwargs.pop("strategies", ["prompt"])

    def generate_with_llm(self, strategy: str, settings: dict, context: dict):
        system_prompt = PromptTemplate.from_package_relative(
            f"{NL2SQL_PROMPT_PREFIX}/generator/{strategy}_system"
        ).apply_prompt_template(**settings)
        user_prompt = PromptTemplate.from_package_relative(
            f"{NL2SQL_PROMPT_PREFIX}/generator/{strategy}_user"
        ).apply_prompt_template(**context)
        prompts = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
        content = llm_manager.get_default_llm().invoke(prompts).content
        self._dump_llm_context(system_prompt, user_prompt, content, self.name, strategy)
        expected_num_sql = settings.get("num_samples", 1) if strategy == "prompt" else 1
        sqls = sql_parser(content)[-expected_num_sql:]
        if self.engine == "postgres":
            sqls = [sql.replace("`", "") for sql in sqls]
        prompt_history = system_prompt + "\n\n" + user_prompt
        return [(sql, prompt_history, strategy) for sql in sqls]

    def strategy_prompt(self, settings, context):
        settings["num_samples"] = self.num_samples
        return self.generate_with_llm("prompt", settings, context)

    def strategy_skeleton(self, settings, context):
        return self.generate_with_llm("skeleton", settings, context)

    def strategy_icl(self, settings, context):
        return self.generate_with_llm("icl", settings, context)

    def strategy_dc(self, settings, context):
        return self.generate_with_llm("dc", settings, context)

    def run_strategy(self, strategy, settings, context):
        fn = getattr(self, f"strategy_{strategy}", None)
        if fn is None:
            raise ValueError(f"Unknown strategy: {strategy}")
        return fn(settings, context)

    def _process(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        settings = {"engine": self.engine}
        context = {
            "question": state["question"],
            "schema": state["schema_str"],
            "sql_rules": state["sql_rules"],
            "evidence": state["evidence"],
            "few_shot_examples": state["few_shot_examples"],
        }
        results = []
        with ThreadPoolExecutor(max_workers=self.num_workers * len(self.strategies)) as executor:
            futures = []
            for strategy in self.strategies:
                for _ in range(self.num_workers):
                    futures.append(submit_in_perf_context(executor, self.run_strategy, strategy, settings, context))
            for future in as_completed(futures):
                res = future.result()
                results.extend(res)
        for i, (sql, prompt, strategy) in enumerate(results):
            state["generation_results"].append(Result(id=i, sql=sql, prompt=prompt, strategy=strategy))
        state["sql"] = state["generation_results"][0].sql
        p = "\n".join([f"[{s.strategy}]\n{s.sql}" for s in state["generation_results"]])
        message = f"=== Generator ===\n{p}"
        logger.info(message)
        state["stream_message"] = message
        return state
