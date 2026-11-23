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
"""Scenario assembly through the formal plugin loader, without ``sys.path`` help.

The in-repo scenario suites put the plugin root on ``sys.path`` for their
own direct ``nl2sql`` imports. That global entry masks the loader's
namespace discipline: in production the base package is reachable only
through temporary ``sys.modules`` aliases, so a broken namespace recovery
in ``nl2sql.hooks`` surfaces as ``ModuleNotFoundError: No module named
'nl2sql'`` at assembly — never in those suites. These tests therefore run
the assembly in a clean subprocess, the production shape, with the plugin
root absent from ``sys.path`` by construction.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT / "dataagent/plugins" / "nl2sql"

HOOK_POINTS = ["generator.post_sql", "perceptor.schema_context", "reflector.post_sql"]

_ASSEMBLY_COMMON = """\
import json
import os
import sys
from pathlib import Path

plugin_root = Path(sys.argv[1]).resolve()
scenario = sys.argv[2]
expected = json.loads(sys.argv[3])

# Production shape: the plugin root is not importable by plain name and
# nothing nl2sql-related is loaded yet.
stray = [p for p in sys.path if p and os.path.realpath(p) == str(plugin_root)]
assert not stray, f"plugin root is importable from sys.path: {stray}"
assert "nl2sql" not in sys.modules

from dataagent.extensions.loading import PythonLoader

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class StubModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "stub"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])


def assemble(entry, scenario):
    \"\"\"Assemble one scenario through an entry module's hooks API.\"\"\"
    config = entry.NL2SQLConfig(
        {
            "model": "primary",
            "scenario": scenario,
            "database": {"db_id": "business_twin", "dialect": "postgres", "engine": "postgres"},
        }
    )
    handlers = entry.load_scenario(
        entry.HookContext(scenario=scenario, model=StubModel(), config=config)
    )
    assert sorted(handlers) == expected, sorted(handlers)
    assert all(callable(handler) for handler in handlers.values())
    return handlers
"""

_SINGLE_LOADER_SCRIPT = (
    _ASSEMBLY_COMMON
    + """\

loader = PythonLoader()
build_subagent = loader.load(plugin_root, "subagents/nl2sql.py:build_subagent")
assert callable(build_subagent)

# After formal loading the base modules live only under the loader namespace.
plain = [key for key in sys.modules if key == "nl2sql" or key.startswith("nl2sql.")]
assert not plain, plain

entry = loader.modules[plugin_root / "subagents" / "nl2sql.py"]
handlers = assemble(entry, scenario)

# The loader's exposure window closed: the plain-name aliases the pack
# imported the base through are gone again.
assert "nl2sql" not in sys.modules

print(json.dumps(sorted(handlers)))
"""
)

_RECOMPILATION_SCRIPT = (
    _ASSEMBLY_COMMON
    + """\

loaders = [PythonLoader() for _ in range(2)]
for loader in loaders:
    loader.load(plugin_root, "subagents/nl2sql.py:build_subagent")

# Two compilations coexist: each hooks copy must recover its own namespace,
# so the key holding a copy is exactly its namespace plus ".nl2sql.hooks".
namespaced = {key: module for key, module in sys.modules.items() if key.endswith(".nl2sql.hooks")}
assert len(namespaced) == 2, sorted(namespaced)
for key, module in namespaced.items():
    assert module._plugin_namespace() + ".nl2sql.hooks" == key, key

# A recompilation must not disturb earlier compilations: every copy still
# assembles, and each binds the pack modules of its own compilation.
seen_modules = set()
for loader in loaders:
    entry = loader.modules[plugin_root / "subagents" / "nl2sql.py"]
    handlers = assemble(entry, scenario)
    origins = {handler.__module__ for handler in handlers.values()}
    assert not origins & seen_modules, origins & seen_modules
    seen_modules |= origins

assert "nl2sql" not in sys.modules
print(json.dumps(sorted(seen_modules)))
"""
)


def _run_assembly(script: str, scenario: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script, str(PLUGIN_ROOT), scenario, json.dumps(HOOK_POINTS)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=PROJECT,
    )


@pytest.mark.parametrize("scenario", ["udn.business_twin", "udn.traffic_insight"])
def test_real_packs_assemble_through_the_formal_loader(scenario: str) -> None:
    """A configured scenario must assemble in a clean process, aliases only.

    Regression test: the namespace used to be recovered from ``__name__``,
    which the formal loader never rewrites, so the plain-name exposure was
    skipped and pack imports failed with ``ModuleNotFoundError``.
    """
    result = _run_assembly(_SINGLE_LOADER_SCRIPT, scenario)
    assert result.returncode == 0, (
        f"scenario {scenario!r} failed to assemble in a clean process:\n{result.stderr}"
    )
    assert json.loads(result.stdout) == HOOK_POINTS


def test_recompilation_keeps_each_assembly_on_its_own_namespace() -> None:
    """Two live compilations must coexist; each hooks copy finds its own namespace.

    Namespace recovery matches module identity, not file paths: copies of
    ``nl2sql.hooks`` share one ``__file__`` while belonging to different
    compilations, so only identity can tell them apart.
    """
    result = _run_assembly(_RECOMPILATION_SCRIPT, "udn.business_twin")
    assert result.returncode == 0, (
        f"recompilation assembly failed in a clean process:\n{result.stderr}"
    )
    # One business_twin assembly per compilation: the selector from
    # table_selector.py and the rewriter from common/dimension_join.py.
    assert len(json.loads(result.stdout)) == 4
