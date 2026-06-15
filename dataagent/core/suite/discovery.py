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
"""Suite discovery: search paths, scan, index."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment
from loguru import logger

from dataagent.core.suite.types import SuiteIndexEntry
from dataagent.utils.runtime_paths import dataagent_home, dataagent_package_path

_SUITE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_RESERVED_SUITE_NAMES: frozenset[str] = frozenset(
    {"hooks", "dataagent", "json", "yaml", "os", "sys", "pathlib", "typing", "suite"}
)


def scan_suite_paths() -> list[Path]:
    """
    Return Suite search roots in priority order (user → builtin_suites).

    与 Skill 类似，仅使用 init 阶段即可确定的稳定路径；**不**扫描 runtime workspace 目录
    （workspace 通常在 ``chat()`` / ``astream()`` 之后才落定，与 ``reload()`` 时机不一致）。

    搜索顺序（同名时先出现的优先）：
    1. ``~/.dataagent/suites/``（或 ``DATAAGENT_HOME/suites/``）— 用户安装，可覆盖内置同名 Suite
    2. ``<dataagent_pkg>/core/suite/builtin_suites/`` — 随包分发的内置 Suite
    """
    paths: list[Path] = [dataagent_home() / "suites"]
    builtin_suites = dataagent_package_path("core", "suite", "builtin_suites")
    if builtin_suites.is_dir():
        paths.append(builtin_suites)
    return paths


def discover_suite_index(
    *,
    config: Mapping[str, Any] | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, SuiteIndexEntry]:
    """
    Scan Suite search paths and build a name → entry index.

    Higher-priority search paths win when the same ``name`` appears multiple times.
    Invalid Suite directories are skipped with warnings.
    """
    index: dict[str, SuiteIndexEntry] = {}
    _ = (config, session_id, user_id)
    search_paths = scan_suite_paths()
    for priority_rank, base in enumerate(search_paths):
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            suite_yaml = child / "suite.yaml"
            if not suite_yaml.is_file():
                continue
            try:
                entry = _load_suite_index_entry(child, suite_yaml)
            except Exception as exc:
                logger.warning("Skipping invalid Suite at {}: {}", child, exc)
                continue
            existing = index.get(entry.name)
            if existing is not None:
                logger.warning(
                    "Duplicate Suite name {!r} at {}; keeping higher-priority path {}",
                    entry.name,
                    child,
                    existing.root,
                )
                continue
            index[entry.name] = entry
            _ = priority_rank
    return index


def _load_suite_index_entry(root: Path, suite_yaml: Path) -> SuiteIndexEntry:
    """Parse and validate one Suite directory for indexing."""
    with open(suite_yaml, encoding="utf-8") as handle:
        meta = yaml.safe_load(handle) or {}
    if not isinstance(meta, Mapping):
        raise ValueError("suite.yaml root must be a mapping")
    name = str(meta.get("name") or "").strip()
    if not name:
        raise ValueError("suite.yaml missing non-empty 'name'")
    _validate_suite_name(name)
    _validate_suite_directory(root, suite_name=name)
    priority = _parse_suite_priority(meta.get("priority"))
    enabled = _parse_suite_enabled(meta.get("enabled"))
    requires = _normalize_requires(meta.get("requires"))
    conflicts = _normalize_name_list(meta.get("conflicts"))
    return SuiteIndexEntry(
        name=name,
        root=root.resolve(),
        priority=priority,
        enabled=enabled,
        requires=requires,
        conflicts=conflicts,
        meta=dict(meta),
    )


def _parse_suite_priority(raw: Any) -> int:
    """
    Parse ``suite.yaml`` ``priority`` as a strict integer.

    Args:
        raw: Raw YAML value for ``priority``.

    Returns:
        Parsed priority, or ``0`` when omitted.

    Raises:
        ValueError: Value is bool or not an integer.
    """
    if raw is None:
        return 0
    if isinstance(raw, bool):
        raise ValueError("suite.yaml priority must be an integer, not bool")
    if not isinstance(raw, int):
        raise ValueError(f"suite.yaml priority must be an integer, got {type(raw).__name__}")
    return raw


def _parse_suite_enabled(raw: Any) -> bool:
    """
    Parse ``suite.yaml`` ``enabled`` as a strict boolean.

    Args:
        raw: Raw YAML value for ``enabled``.

    Returns:
        Parsed enabled flag; defaults to ``True`` when omitted.

    Raises:
        ValueError: Value is not a boolean.
    """
    if raw is None:
        return True
    if not isinstance(raw, bool):
        raise ValueError(f"suite.yaml enabled must be a boolean, got {type(raw).__name__}")
    return raw


def _validate_suite_name(name: str) -> None:
    """Ensure Suite name is a valid Python identifier and not reserved."""
    if not _SUITE_NAME_RE.match(name):
        raise ValueError(f"Suite name must be a valid Python identifier: {name!r}")
    if name in _RESERVED_SUITE_NAMES:
        raise ValueError(f"Suite name is reserved: {name!r}")


def _validate_suite_directory(root: Path, *, suite_name: str) -> None:
    """Validate optional Suite bundle files under ``root``."""
    tools_yaml = root / "tools" / "tools.yaml"
    if tools_yaml.is_file():
        with open(tools_yaml, encoding="utf-8") as handle:
            tools_doc = yaml.safe_load(handle) or {}
        if isinstance(tools_doc, Mapping):
            tools = tools_doc.get("TOOLS")
            if isinstance(tools, Mapping):
                if "skills" in tools:
                    raise ValueError("Suite tools/tools.yaml must not contain TOOLS.skills")
                local_functions = tools.get("local_functions") or []
                if isinstance(local_functions, Sequence) and not isinstance(local_functions, (str, bytes)):
                    for item in local_functions:
                        if isinstance(item, Mapping):
                            fn = item.get("function") or item.get("name")
                            if str(fn or "").strip() == "sub_agent_tool":
                                raise ValueError("Suite tools/tools.yaml must not declare sub_agent_tool")

    prompts_dir = root / "prompts"
    if prompts_dir.is_dir():
        allowed = {"system", "user"}
        for child in prompts_dir.iterdir():
            if child.name in allowed:
                continue
            if child.is_file():
                raise ValueError(f"prompts/ must not contain files at root: {child.name}")
            raise ValueError(f"prompts/ may only contain system/ and user/ subdirectories, got: {child.name}")
        env = Environment()
        for sub in ("system", "user"):
            subdir = prompts_dir / sub
            if not subdir.is_dir():
                continue
            for template in subdir.rglob("*"):
                if not template.is_file():
                    continue
                if template.suffix.lower() not in {".md", ".j2", ".jinja", ".jinja2", ".txt"}:
                    continue
                env.parse(template.read_text(encoding="utf-8"))

    skills_dir = root / "skills"
    if skills_dir.is_dir():
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir() and not (skill_dir / "SKILL.md").is_file():
                raise ValueError(f"skills/{skill_dir.name}/ must contain SKILL.md")

    subagents_dir = root / "subagents"
    if subagents_dir.is_dir():
        for item in subagents_dir.iterdir():
            if not item.is_file():
                continue
            if item.suffix.lower() not in {".yaml", ".yml"}:
                continue
            with open(item, encoding="utf-8") as handle:
                doc = yaml.safe_load(handle) or {}
            if not isinstance(doc, Mapping):
                raise ValueError(f"subagents/{item.name} root must be a mapping")
            agent_cfg = doc.get("AGENT_CONFIG")
            if not isinstance(agent_cfg, Mapping):
                raise ValueError(f"subagents/{item.name} must contain AGENT_CONFIG")
            if not str(agent_cfg.get("description") or "").strip():
                raise ValueError(f"subagents/{item.name} missing AGENT_CONFIG.description")

    _validate_suite_hooks(root, suite_name=suite_name)


def _validate_suite_hooks(root: Path, *, suite_name: str) -> None:
    """Validate ``hooks/hooks.yaml`` hook entries for Suite authoring rules."""
    hooks_yaml = root / "hooks" / "hooks.yaml"
    if not hooks_yaml.is_file():
        return
    with open(hooks_yaml, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    if not isinstance(doc, Mapping):
        raise ValueError("hooks/hooks.yaml root must be a mapping")
    hooks = doc.get("HOOKS")
    if hooks is None:
        return
    if not isinstance(hooks, Mapping):
        raise ValueError("hooks/hooks.yaml HOOKS must be a mapping")
    _validate_suite_hooks_node(hooks, suite_name=suite_name, path="HOOKS")


def _validate_suite_hooks_node(node: Any, *, suite_name: str, path: str) -> None:
    """Recursively validate HOOKS mapping nodes under ``hooks/hooks.yaml``."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            _validate_suite_hooks_node(value, suite_name=suite_name, path=f"{path}.{key}")
        return
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for idx, item in enumerate(node):
            _validate_suite_hook_item(item, suite_name=suite_name, path=f"{path}[{idx}]")
        return
    raise ValueError(f"{path}: invalid HOOKS node type")


def _validate_suite_hook_item(item: Any, *, suite_name: str, path: str) -> None:
    """Validate one Suite hook list entry (string or dict with ``name``)."""
    from dataagent.core.flex.hooks.registry import BUILTIN_HOOK_REGISTRY

    if isinstance(item, str):
        spec = item.strip()
        if not spec:
            raise ValueError(f"{path}: empty hook spec")
        if spec in BUILTIN_HOOK_REGISTRY:
            raise ValueError(f"{path}: builtin short name {spec!r} is not allowed in Suite hooks")
        if spec.startswith(f"{suite_name}."):
            raise ValueError(f"{path}: must not include suite name prefix {suite_name!r}")
        _validate_suite_hook_dotted_path(spec, path=path)
        return
    if isinstance(item, Mapping):
        raw_name = str(item.get("name") or "").strip()
        if not raw_name:
            raise ValueError(f"{path}: hook dict missing non-empty 'name'")
        if raw_name in BUILTIN_HOOK_REGISTRY:
            raise ValueError(f"{path}: builtin short name {raw_name!r} is not allowed in Suite hooks")
        if raw_name.startswith(f"{suite_name}."):
            raise ValueError(f"{path}: must not include suite name prefix {suite_name!r}")
        _validate_suite_hook_dotted_path(raw_name, path=path)
        return
    raise ValueError(f"{path}: invalid hook entry type")


def _validate_suite_hook_dotted_path(spec: str, *, path: str) -> None:
    """Ensure a Suite hook spec has at least ``pkg.module.callable`` shape."""
    parts = [part for part in spec.split(".") if part]
    if len(parts) < 3:
        raise ValueError(f"{path}: hook spec must be module.path.callable, got {spec!r}")


def _normalize_requires(raw: Any) -> tuple[str, ...]:
    """Normalize ``requires`` entries to Suite name strings."""
    if not raw:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("suite.yaml requires must be a list")
    names: list[str] = []
    for item in raw:
        if isinstance(item, str):
            names.append(item.strip())
        elif isinstance(item, Mapping):
            name = str(item.get("name") or "").strip()
            if name:
                names.append(name)
    return tuple(n for n in names if n)


def _normalize_name_list(raw: Any) -> tuple[str, ...]:
    """Normalize a list of Suite name strings."""
    if not raw:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("suite.yaml list field must be a list of names")
    return tuple(str(item).strip() for item in raw if str(item).strip())
