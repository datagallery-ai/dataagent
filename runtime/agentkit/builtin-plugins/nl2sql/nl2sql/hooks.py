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
"""Scenario packs: named hook points, the assembly context, and the loader.

The base stays scenario-knowledge-free: nodes query hook points and
fall back to default behavior when none is registered. A pack is a
directory ``scenarios/<dotted name>/`` whose ``scenario.py`` defines
``build_scenario(ctx) -> {hook point: handler}``; registration may be
partial. Handlers are closures that receive only their payload —
everything else is captured at assembly — and the loader lets packs
import base modules under plain names without leaking them into
``sys.modules``.
"""

from __future__ import annotations

import hashlib
import importlib
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from langchain_core.language_models import BaseChatModel

from nl2sql.config_manager import NL2SQLConfig
from nl2sql.state import Result

if TYPE_CHECKING:
    from dataagent.semantic_client import SemanticServiceClient

PERCEPTOR_SCHEMA_CONTEXT = "perceptor.schema_context"
"""Hook point: produce the schema context (default: schema linking or full schema)."""

GENERATOR_POST_SQL = "generator.post_sql"
"""Hook point: rewrite the generator's SQL candidates."""

REFLECTOR_POST_SQL = "reflector.post_sql"
"""Hook point: rewrite the reflector's repaired SQL."""

HOOK_POINTS = frozenset({PERCEPTOR_SCHEMA_CONTEXT, GENERATOR_POST_SQL, REFLECTOR_POST_SQL})
"""Every point a pack may register; adding one is a base change."""


@dataclass(frozen=True)
class SchemaContext:
    """The perceptor hook's return value: the schema slice the run sees.

    Args:
        schema: Selected tables (``{table: {description, columns}}``).
        joins: Joinable table pairs, as ``(src_table, target_table)``.
        schema_str: Handler-made DDL; unlike the default flow it may embed
            derived-metric formulas.
        extra_rules: The sole rule-injection channel, appended to ``sql_rules``.
    """
    schema: dict[str, Any]
    joins: list[tuple[str, str]] = field(default_factory=list)
    schema_str: str = ""
    extra_rules: str = ""


SchemaContextHandler = Callable[[str], Awaitable[SchemaContext]]
"""``async (question) -> SchemaContext``."""

PostSqlHandler = Callable[[list[Result], dict[str, Any]], Awaitable[list[Result]]]
"""``async (results, state) -> list[Result]``."""

Handler = SchemaContextHandler | PostSqlHandler
"""Any scenario handler."""


@dataclass(frozen=True)
class HookContext:
    """Everything a pack may close over at assembly time.

    Args:
        scenario: Dotted pack name (e.g. ``udn.business_twin``).
        model: Budgeted delegate; calls through it count against the task budget.
        config: Validated plugin config (dotted-key access).
        semantic_client: Host-owned semantic client, ``None`` when the root
            ``semantic`` settings are absent.
    """
    scenario: str
    model: BaseChatModel
    config: NL2SQLConfig
    semantic_client: SemanticServiceClient | None = None


def load_scenario(ctx: HookContext, *, scenario_root: Path | None = None) -> dict[str, Handler]:
    """Assemble the pack named ``ctx.scenario`` into a validated handler dict.

    The pack module executes once per compilation while ``build_scenario``
    runs on every assembly, so per-task state belongs in the closures it
    builds.

    Args:
        ctx: Assembly context for the pack to close over.
        scenario_root: Overrides the pack tree location (tests).

    Returns:
        A copied ``{hook point: handler}`` mapping; unregistered points
        keep their default behavior.
    """
    root = (scenario_root if scenario_root is not None else _DEFAULT_SCENARIO_ROOT).resolve()
    segments = _scenario_segments(ctx.scenario)
    entry = root.joinpath(*segments) / "scenario.py"
    if not entry.is_file():
        raise ValueError(f"scenario {ctx.scenario!r} has no pack: expected entry {entry}")
    module = _import_pack(root, segments)
    build = getattr(module, "build_scenario", None)
    if not callable(build):
        raise ValueError(
            f"scenario pack {ctx.scenario!r} is missing its entry function: "
            f"{entry} must define build_scenario(ctx)"
        )
    handlers = build(ctx)
    if not isinstance(handlers, Mapping):
        raise ValueError(
            f"build_scenario of scenario {ctx.scenario!r} must return a "
            f"{{hook point: handler}} mapping, got {type(handlers).__name__}"
        )
    unknown = sorted(set(handlers) - HOOK_POINTS)
    if unknown:
        raise ValueError(
            f"scenario {ctx.scenario!r} registered unknown hook point(s) {unknown}; "
            f"valid points: {sorted(HOOK_POINTS)}"
        )
    for point, handler in handlers.items():
        if not callable(handler):
            raise ValueError(
                f"handler of scenario {ctx.scenario!r} for hook point {point!r} "
                f"is not callable: {handler!r}"
            )
    return dict(handlers)


_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
"""The nl2sql plugin root: base modules under it, packs under ``scenarios/``."""

_DEFAULT_SCENARIO_ROOT = _PLUGIN_ROOT / "scenarios"


def _scenario_segments(scenario: str) -> list[str]:
    """Split a dotted scenario name into segments, rejecting anything path-like.

    Args:
        scenario: Dotted pack name; each segment must be an identifier.

    Returns:
        The directory segments under the scenario root.
    """
    if not isinstance(scenario, str) or not scenario.strip():
        raise ValueError(f"scenario must be a non-empty dotted name, got {scenario!r}")
    segments = scenario.split(".")
    for segment in segments:
        # Identifier segments cannot contain separators, so joinpath can never escape the root.
        if not segment.isidentifier():
            raise ValueError(
                f"invalid scenario name {scenario!r}: segment {segment!r} is not an identifier"
            )
    return segments


_SELF = sys.modules.get(__name__)
"""This module object, captured while its registration key is still plain."""


def _plugin_namespace() -> str:
    """The host plugin namespace this module runs under.

    Packs import the base package under its plain name, so the loader
    needs the prefix to expose and re-namespace those modules.

    Returns:
        The namespace prefix, ``""`` when this module is not namespaced.
    """
    if _SELF is None:
        return ""
    suffix = ".nl2sql.hooks"
    for key, module in list(sys.modules.items()):
        if key.endswith(suffix) and module is _SELF:
            return key[: -len(suffix)]
    return ""


def _packs_package(scenario_root: Path) -> str:
    """Top package name for one scenario root.

    Args:
        scenario_root: The packs tree; each root gets its own digest suffix.

    Returns:
        A single-segment name under the host namespace. ``import_module``
        walks parent packages, and the host namespace registers no bare
        parent module.
    """
    namespace = _plugin_namespace()
    digest = hashlib.sha256(str(scenario_root).encode()).hexdigest()[:16]
    prefix = namespace if namespace else "_nl2sql"
    return f"{prefix}_scenarios_{digest}"


def _ensure_namespace_package(name: str, path: Path) -> None:
    """Register a namespace package in ``sys.modules``; reuse if present.

    Args:
        name: Module name to register.
        path: Directory the package's ``__path__`` points to.
    """
    existing = sys.modules.get(name)
    if existing is not None:
        if not hasattr(existing, "__path__"):
            # Unreachable unless the interpreter state is already corrupted.
            raise RuntimeError(f"module name {name!r} is already used by a non-package module")
        return
    package = ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package


def _import_pack(scenario_root: Path, segments: list[str]) -> ModuleType:
    """Import a pack's ``scenario`` module under the loader's namespace discipline.

    During the import, host-namespaced base modules are exposed under
    their plain names so ``from nl2sql... import`` in the pack binds the
    same module objects the base uses. Afterwards the aliases are dropped
    and any import-created module with an origin inside the plugin root
    is renamed back into the host namespace; pack modules stay put — the
    per-compilation top name already isolates them.

    Args:
        scenario_root: Location of the packs tree.
        segments: The pack's directory segments under the root.

    Returns:
        The imported ``scenario`` module.
    """
    top = _packs_package(scenario_root)
    _ensure_namespace_package(top, scenario_root)
    namespace = _plugin_namespace()
    prefix = f"{namespace}." if namespace else ""
    exposed: list[str] = []
    if prefix:
        # Mirror of the host loader's exposure step; entrypoint modules stay excluded.
        exposed = [
            key
            for key in sys.modules
            if key.startswith(prefix)
            and not key[len(prefix):].startswith("_entry_")
            and key[len(prefix):] not in sys.modules
        ]
        for key in exposed:
            sys.modules[key[len(prefix):]] = sys.modules[key]
    before = set(sys.modules)
    try:
        return importlib.import_module(".".join([top, *segments, "scenario"]))
    finally:
        for key in exposed:
            sys.modules.pop(key[len(prefix):], None)
        if prefix:
            resolved_root = _PLUGIN_ROOT.resolve()
            for key in list(sys.modules):
                if key in before or key.startswith(prefix) or key == top or key.startswith(top + "."):
                    continue
                origin = getattr(sys.modules[key], "__file__", None)
                if origin and Path(origin).resolve().is_relative_to(resolved_root):
                    sys.modules[prefix + key] = sys.modules.pop(key)
