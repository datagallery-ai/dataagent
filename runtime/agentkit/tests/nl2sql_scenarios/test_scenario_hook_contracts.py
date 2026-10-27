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
"""Node-level contracts for the three scenario hook points.

The failure contract is held by the nodes, not the handlers: a handler may
raise freely. Perceptor has no fallback (a failed selection is fatal);
generator keeps the original candidates; reflector keeps the repaired SQL
flowing on unchanged. A registered hook must not fire when the user
supplied their own schema.
"""

import pytest
from nl2sql.config_manager import NL2SQLConfig
from nl2sql.errors import DataAgentError
from nl2sql.hooks import (
    GENERATOR_POST_SQL,
    PERCEPTOR_SCHEMA_CONTEXT,
    REFLECTOR_POST_SQL,
    SchemaContext,
)
from nl2sql.nodes.generator import GeneratorNode
from nl2sql.nodes.perceptor import PerceptorNode
from nl2sql.nodes.reflector import ReflectorNode
from nl2sql.state import Result

from nl2sql_scenarios.support import scripted_model


def make_config() -> NL2SQLConfig:
    return NL2SQLConfig(
        {
            "model": "primary",
            "database": {"db_id": "test", "dialect": "postgres"},
        }
    )


def perceptor_state(**overrides) -> dict:
    state = {
        "question": "无锡市的小区上行PRB利用率",
        "context_overrides": [],
        "schema": {},
        "joins": [],
        "schema_str": "",
        "sql_rules": "",
        "evidence": "",
        "few_shot_examples": "",
    }
    state.update(overrides)
    return state


# --- perceptor.schema_context -------------------------------------------------


async def test_perceptor_hook_supplies_schema_context() -> None:
    async def select(question: str) -> SchemaContext:
        assert question == "无锡市的小区上行PRB利用率"
        return SchemaContext(
            schema={"t": {"description": "", "columns": {}}},
            joins=[("t", "d")],
            schema_str="CREATE TABLE t (id INT);",
            extra_rules="## 场景规则",
        )

    node = PerceptorNode(config=make_config())
    node.bind_hooks({PERCEPTOR_SCHEMA_CONTEXT: select})
    state = await node.run(perceptor_state())

    assert state["schema"] == {"t": {"description": "", "columns": {}}}
    assert state["joins"] == [("t", "d")]
    assert state["schema_str"] == "CREATE TABLE t (id INT);"
    assert state["sql_rules"] == "## 场景规则"


async def test_perceptor_hook_appends_extra_rules_to_existing_rules() -> None:
    async def select(question: str) -> SchemaContext:
        return SchemaContext(
            schema={}, schema_str="CREATE TABLE t (id INT);", extra_rules="## 场景规则"
        )

    node = PerceptorNode(config=make_config())
    node.bind_hooks({PERCEPTOR_SCHEMA_CONTEXT: select})
    state = perceptor_state(sql_rules="base rules", context_overrides=["sql_rules"])
    state = await node.run(state)

    assert state["sql_rules"] == "base rules\n## 场景规则"


async def test_perceptor_hook_empty_schema_str_is_a_config_error() -> None:
    async def select(question: str) -> SchemaContext:
        return SchemaContext(schema={}, schema_str="")

    node = PerceptorNode(config=make_config())
    node.bind_hooks({PERCEPTOR_SCHEMA_CONTEXT: select})
    with pytest.raises(DataAgentError) as excinfo:
        await node.run(perceptor_state())
    assert excinfo.value.source == "config"


async def test_perceptor_hook_errors_propagate_without_fallback() -> None:
    async def select(question: str) -> SchemaContext:
        raise RuntimeError("selection exploded")

    node = PerceptorNode(config=make_config())
    node.bind_hooks({PERCEPTOR_SCHEMA_CONTEXT: select})
    with pytest.raises(RuntimeError, match="selection exploded"):
        await node.run(perceptor_state())


async def test_perceptor_hook_not_consulted_when_user_supplied_schema() -> None:
    calls = []

    async def select(question: str) -> SchemaContext:
        calls.append(question)
        raise AssertionError("must not be called")

    node = PerceptorNode(config=make_config())
    node.bind_hooks({PERCEPTOR_SCHEMA_CONTEXT: select})
    user_schema = {"employees": {"description": "", "columns": {"department": {}}}}
    state = perceptor_state(schema=user_schema, context_overrides=["schema"])
    state = await node.run(state)

    assert calls == []
    assert state["schema"] == user_schema
    assert "employees" in state["schema_str"]  # derived from the user-supplied schema


async def test_perceptor_without_hook_keeps_default_flow() -> None:
    """No hook and no semantic client: the default flow's own config error, not a hook error."""
    node = PerceptorNode(config=make_config())
    with pytest.raises(DataAgentError, match="semantic 服务未配置"):
        await node.run(perceptor_state())


# --- generator.post_sql -------------------------------------------------------


def generator_state() -> dict:
    return {
        "question": "各部门员工数",
        "sql_rules": "",
        "schema_str": "CREATE TABLE employees (department TEXT);",
        "evidence": "",
        "few_shot_examples": "",
        "generation_results": [],
    }


def make_generator(hooks: dict | None) -> GeneratorNode:
    node = GeneratorNode(config=make_config(), strategies=["prompt"], num_workers=1)
    node.bind_model(scripted_model("```sql\nSELECT department FROM employees\n```"))
    node.bind_hooks(hooks)
    return node


async def test_generator_hook_rewrites_candidates() -> None:
    async def rewrite(results, state):
        assert state["question"] == "各部门员工数"
        return [Result(id=99, sql="SELECT rewritten")]

    node = make_generator({GENERATOR_POST_SQL: rewrite})
    state = await node.run(generator_state())

    assert [r.sql for r in state["generation_results"]] == ["SELECT rewritten"]
    assert state["generation_results"][0].id == 0  # renumbered after rewrite
    assert state["sql"] == "SELECT rewritten"


async def test_generator_hook_failure_keeps_original_candidates() -> None:
    async def rewrite(results, state):
        raise RuntimeError("rewrite exploded")

    node = make_generator({GENERATOR_POST_SQL: rewrite})
    state = await node.run(generator_state())

    assert [r.sql for r in state["generation_results"]] == ["SELECT department FROM employees"]
    assert state["sql"] == "SELECT department FROM employees"


async def test_generator_hook_empty_rewrite_keeps_original_candidates() -> None:
    async def rewrite(results, state):
        return []

    node = make_generator({GENERATOR_POST_SQL: rewrite})
    state = await node.run(generator_state())

    assert [r.sql for r in state["generation_results"]] == ["SELECT department FROM employees"]


async def test_generator_without_hook_passes_candidates_through() -> None:
    node = make_generator(None)
    state = await node.run(generator_state())

    assert [r.sql for r in state["generation_results"]] == ["SELECT department FROM employees"]


# --- reflector.post_sql -------------------------------------------------------


def reflector_state() -> dict:
    failed = Result(id=0, sql="SELECT bad FROM employees", score=0.3, issues=["unknown column"])
    return {
        "validation_results": [failed],
        "generation_results": [],
        "ref_retries": 2,
        "proceed": False,
        "security_sql_approved": False,
    }


def make_reflector(hooks: dict | None) -> ReflectorNode:
    node = ReflectorNode(config=make_config(), threshold=0.9)
    node.bind_model(scripted_model('```json\n[{"sql": "SELECT fixed FROM employees"}]\n```'))
    node.bind_hooks(hooks)
    return node


async def test_reflector_hook_rewrites_repaired_sql() -> None:
    async def rewrite(results, state):
        assert state["ref_retries"] == 1  # one retry consumed by the repair round
        return [Result(id=0, sql="SELECT fixed JOINED")]

    node = make_reflector({REFLECTOR_POST_SQL: rewrite})
    state = await node.run(reflector_state())

    assert [r.sql for r in state["generation_results"]] == ["SELECT fixed JOINED"]
    assert state["validation_results"] == []


async def test_reflector_hook_failure_keeps_repaired_sql() -> None:
    async def rewrite(results, state):
        raise RuntimeError("rewrite exploded")

    node = make_reflector({REFLECTOR_POST_SQL: rewrite})
    state = await node.run(reflector_state())

    assert [r.sql for r in state["generation_results"]] == ["SELECT fixed FROM employees"]
    # Fail-open like the generator hook: no need_ref, the repair re-enters the
    # normal validate-and-accept flow instead of burning ref_retries.
    assert all(not result.need_ref for result in state["generation_results"])


async def test_reflector_hook_empty_rewrite_keeps_repaired_sql() -> None:
    async def rewrite(results, state):
        return []

    node = make_reflector({REFLECTOR_POST_SQL: rewrite})
    state = await node.run(reflector_state())

    assert [r.sql for r in state["generation_results"]] == ["SELECT fixed FROM employees"]
    assert all(not result.need_ref for result in state["generation_results"])
