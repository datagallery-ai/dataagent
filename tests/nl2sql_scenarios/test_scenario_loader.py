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
"""Loader and configuration tests for the scenario-pack mechanism.

``load_scenario`` is the assembly-time gate: every way a pack can be wrong
(missing entry, wrong return type, unknown hook point, non-callable handler,
path-like name) must fail at assembly with a named error, never at run time.
Valid packs — including partial registrations — assemble into a fresh handler
dict whose module executes once per compilation.
"""

from pathlib import Path
from textwrap import dedent

import pytest
from nl2sql.config_manager import NL2SQLConfig
from nl2sql.hooks import (
    GENERATOR_POST_SQL,
    HOOK_POINTS,
    PERCEPTOR_SCHEMA_CONTEXT,
    REFLECTOR_POST_SQL,
    load_scenario,
)
from nl2sql.nodes.generator import GENERATOR_POST_SQL as NODE_GENERATOR_POST_SQL
from nl2sql.nodes.perceptor import PERCEPTOR_SCHEMA_CONTEXT as NODE_PERCEPTOR_SCHEMA_CONTEXT
from nl2sql.nodes.reflector import REFLECTOR_POST_SQL as NODE_REFLECTOR_POST_SQL

from nl2sql_scenarios.support import ScriptedModel, make_context

SCENARIO_ROOT = Path(__file__).resolve().parents[2] / "dataagent/plugins" / "nl2sql" / "scenarios"


def write_pack(root: Path, name: str, body: str) -> Path:
    """Create a minimal pack tree with the given ``scenario.py`` body."""
    pack = root / name
    pack.mkdir(parents=True)
    (pack / "scenario.py").write_text(dedent(body), encoding="utf-8")
    return pack


MINIMAL_PACK = """
    from nl2sql.hooks import GENERATOR_POST_SQL

    async def rewrite(results, state):
        return results

    def build_scenario(ctx):
        return {GENERATOR_POST_SQL: rewrite}
"""


def test_unknown_pack_fails_assembly() -> None:
    with pytest.raises(ValueError, match="has no pack"):
        load_scenario(make_context("udn.nonexistent", ScriptedModel(responses=[]), None))


def test_namespace_directory_without_entry_is_not_a_pack() -> None:
    """``common/`` holds shared assets and must never assemble as a scenario."""
    with pytest.raises(ValueError, match="has no pack"):
        load_scenario(make_context("udn.common", ScriptedModel(responses=[]), None))


def test_missing_entry_function_fails_assembly(tmp_path: Path) -> None:
    write_pack(
        tmp_path,
        "s1",
        """
        ANSWER = 42
        """,
    )
    with pytest.raises(ValueError, match="missing its entry function"):
        load_scenario(make_context("s1", ScriptedModel(responses=[]), None), scenario_root=tmp_path)


def test_non_mapping_return_fails_assembly(tmp_path: Path) -> None:
    write_pack(
        tmp_path,
        "s1",
        """
        def build_scenario(ctx):
            return [None]
        """,
    )
    with pytest.raises(ValueError, match="must return a"):
        load_scenario(make_context("s1", ScriptedModel(responses=[]), None), scenario_root=tmp_path)


def test_unknown_hook_point_fails_assembly(tmp_path: Path) -> None:
    write_pack(
        tmp_path,
        "s1",
        """
        async def handler(results, state):
            return results

        def build_scenario(ctx):
            return {"perceptor.pre_sql": handler}
        """,
    )
    with pytest.raises(ValueError, match="unknown hook point"):
        load_scenario(make_context("s1", ScriptedModel(responses=[]), None), scenario_root=tmp_path)


def test_non_callable_handler_fails_assembly(tmp_path: Path) -> None:
    write_pack(
        tmp_path,
        "s1",
        """
        from nl2sql.hooks import GENERATOR_POST_SQL

        def build_scenario(ctx):
            return {GENERATOR_POST_SQL: "not callable"}
        """,
    )
    with pytest.raises(ValueError, match="is not callable"):
        load_scenario(make_context("s1", ScriptedModel(responses=[]), None), scenario_root=tmp_path)


@pytest.mark.parametrize(
    "scenario",
    ["a/b", "../escape", "udn..x", "with space", "a.b/c", "ends."],
)
def test_path_like_or_malformed_names_are_rejected(scenario: str) -> None:
    """Each dotted segment must be an identifier, so the name can never escape the root."""
    with pytest.raises(ValueError, match="not an identifier|non-empty"):
        load_scenario(make_context(scenario, ScriptedModel(responses=[]), None))


def test_partial_registration_assembles(tmp_path: Path) -> None:
    """A pack may register a subset of the hook points; the rest keep default behavior."""
    write_pack(tmp_path, "s1", MINIMAL_PACK)
    handlers = load_scenario(
        make_context("s1", ScriptedModel(responses=[]), None), scenario_root=tmp_path
    )
    assert set(handlers) == {GENERATOR_POST_SQL}


def test_pack_module_executes_once_per_compilation(tmp_path: Path) -> None:
    write_pack(
        tmp_path,
        "counter",
        """
        from pathlib import Path

        from nl2sql.hooks import GENERATOR_POST_SQL

        marker = Path(__file__).parent / "imports.txt"
        marker.write_text(str(int(marker.read_text()) + 1 if marker.exists() else 1))

        async def rewrite(results, state):
            return results

        def build_scenario(ctx):
            return {GENERATOR_POST_SQL: rewrite}
        """,
    )
    ctx = make_context("counter", ScriptedModel(responses=[]), None)
    load_scenario(ctx, scenario_root=tmp_path)
    load_scenario(ctx, scenario_root=tmp_path)
    assert (tmp_path / "counter" / "imports.txt").read_text() == "1"


def test_returned_mapping_is_a_fresh_copy(tmp_path: Path) -> None:
    """Mutating one assembly's handler dict must not affect the next."""
    write_pack(tmp_path, "s1", MINIMAL_PACK)
    ctx = make_context("s1", ScriptedModel(responses=[]), None)
    first = load_scenario(ctx, scenario_root=tmp_path)
    first.pop(GENERATOR_POST_SQL)
    second = load_scenario(ctx, scenario_root=tmp_path)
    assert set(second) == {GENERATOR_POST_SQL}


@pytest.mark.parametrize("scenario", ["udn.business_twin", "udn.traffic_insight"])
def test_real_packs_register_every_hook_point(scenario: str) -> None:
    handlers = load_scenario(make_context(scenario, ScriptedModel(responses=[]), None))
    assert set(handlers) == HOOK_POINTS
    assert all(callable(handler) for handler in handlers.values())


def test_hook_point_constants_are_shared_with_nodes() -> None:
    """Nodes and packs must agree on point names — one source of truth in hooks.py."""
    assert NODE_PERCEPTOR_SCHEMA_CONTEXT is PERCEPTOR_SCHEMA_CONTEXT
    assert NODE_GENERATOR_POST_SQL is GENERATOR_POST_SQL
    assert NODE_REFLECTOR_POST_SQL is REFLECTOR_POST_SQL
    assert HOOK_POINTS == frozenset(
        {PERCEPTOR_SCHEMA_CONTEXT, GENERATOR_POST_SQL, REFLECTOR_POST_SQL}
    )


# --- configuration: the ``scenario`` key -------------------------------------


def base_settings() -> dict:
    return {
        "model": "primary",
        "database": {"db_id": "business_twin", "dialect": "postgres"},
    }


def test_config_accepts_dotted_scenario_name() -> None:
    config = NL2SQLConfig({**base_settings(), "scenario": "udn.business_twin"})
    assert config.scenario == "udn.business_twin"


def test_config_scenario_defaults_to_none() -> None:
    assert NL2SQLConfig(base_settings()).scenario is None


@pytest.mark.parametrize("scenario", ["", "   ", 42, ["udn"]])
def test_config_rejects_malformed_scenario(scenario: object) -> None:
    with pytest.raises(ValueError, match="dotted scenario name"):
        NL2SQLConfig({**base_settings(), "scenario": scenario})


def test_config_still_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown"):
        NL2SQLConfig({**base_settings(), "perceptor_type": "business_twin"})
