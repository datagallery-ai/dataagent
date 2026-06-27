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
"""Suite activation: include, requires closure, conflicts, sorting."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from loguru import logger

from dataagent.suite.types import SuiteIndexEntry, SuiteRecord


def _parse_priority_override(raw: Any, *, field_path: str) -> int | None:
    """
    Parse ``SUITE.include`` ``priority_override`` as a strict integer.

    Args:
        raw: Raw YAML value for ``priority_override``.
        field_path: Human-readable field path for error messages.

    Returns:
        Parsed integer, or ``None`` when the key is omitted.

    Raises:
        ValueError: Value is bool or not an integer.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{field_path}: priority_override must be an integer, not bool")
    if not isinstance(raw, int):
        raise ValueError(f"{field_path}: priority_override must be an integer, got {type(raw).__name__}")
    return raw


def activate_suites(
    *,
    suite_config: Mapping[str, Any] | None,
    index: Mapping[str, SuiteIndexEntry],
) -> list[SuiteRecord]:
    """
    Resolve ``SUITE.include`` into an ordered activation list.

    Args:
        suite_config: User ``SUITE`` mapping (or None when absent).
        index: Discovered Suite index.

    Returns:
        Activated suites sorted low → high priority for ``merge_layers``.

    Raises:
        ValueError: Activation, dependency, or conflict rules violated.
    """
    if not suite_config:
        return []
    include = suite_config.get("include")
    if not include:
        return []
    if not isinstance(include, Sequence) or isinstance(include, (str, bytes)):
        raise ValueError("SUITE.include must be a list")

    explicit: dict[str, int | None] = {}
    for item in include:
        if isinstance(item, str):
            explicit[item.strip()] = None
        elif isinstance(item, Mapping):
            name = str(item.get("name") or "").strip()
            if not name:
                raise ValueError("SUITE.include object entries require 'name'")
            override = _parse_priority_override(
                item.get("priority_override"),
                field_path=f"SUITE.include[{name!r}].priority_override",
            )
            explicit[name] = override
        else:
            raise ValueError("SUITE.include items must be strings or mappings")

    activated_names: set[str] = set()
    required_by: dict[str, set[str]] = {}

    def _ensure(name: str, *, via_requires: bool) -> None:
        if name in activated_names:
            return
        entry = index.get(name)
        if entry is None:
            raise ValueError(f"Suite not found: {name!r}")
        if not entry.enabled:
            if via_requires:
                raise ValueError(f"Required Suite {name!r} is disabled (enabled: false)")
            logger.warning("Suite {!r} is disabled (enabled: false); skipping explicit include", name)
            return
        activated_names.add(name)
        for dep in entry.requires:
            required_by.setdefault(dep, set()).add(name)
            _ensure(dep, via_requires=True)

    for name in explicit:
        _ensure(name, via_requires=False)

    for name in list(activated_names):
        entry = index[name]
        if entry.name in entry.conflicts:
            raise ValueError(f"Suite {entry.name!r} conflicts with itself")
        overlap = activated_names.intersection(set(entry.conflicts))
        if overlap:
            raise ValueError(f"Suite {entry.name!r} conflicts with activated: {sorted(overlap)}")

    _detect_requires_cycle(activated_names, index)

    records: list[SuiteRecord] = []
    for name in activated_names:
        entry = index[name]
        priority_override = explicit.get(name)
        priority = int(priority_override) if priority_override is not None else entry.priority
        records.append(
            SuiteRecord(
                name=entry.name,
                root=entry.root,
                priority=priority,
                enabled=entry.enabled,
                requires=entry.requires,
                conflicts=entry.conflicts,
                meta=entry.meta,
            )
        )
    records.sort(key=lambda rec: rec.priority)
    grouped: list[SuiteRecord] = []
    current_priority: int | None = None
    bucket: list[SuiteRecord] = []
    for rec in records:
        if current_priority is None or rec.priority != current_priority:
            if bucket:
                grouped.extend(sorted(bucket, key=lambda item: item.name))
            bucket = [rec]
            current_priority = rec.priority
        else:
            bucket.append(rec)
    if bucket:
        grouped.extend(sorted(bucket, key=lambda item: item.name))
    return grouped


def order_suites_for_merge(records: Sequence[SuiteRecord]) -> list[SuiteRecord]:
    """
    Reorder activated suites for ``merge_layers`` while keeping activation semantics.

    ``activate_suites`` returns ``(priority 升序, name 字母序升序)`` for stable display.
    Within each priority bucket, reverse name order so list-merge output places
    lexicographically smaller names first (a before z).

    Args:
        records: Output of :func:`activate_suites` (low → high priority, name asc).

    Returns:
        Same records reordered for ``build_suite_layers`` / ``merge_layers``.
    """
    if not records:
        return []
    ordered: list[SuiteRecord] = []
    current_priority: int | None = None
    bucket: list[SuiteRecord] = []
    for rec in records:
        if current_priority is None or rec.priority != current_priority:
            if bucket:
                ordered.extend(reversed(bucket))
            bucket = [rec]
            current_priority = rec.priority
        else:
            bucket.append(rec)
    if bucket:
        ordered.extend(reversed(bucket))
    return ordered


def _detect_requires_cycle(names: set[str], index: Mapping[str, SuiteIndexEntry]) -> None:
    """Raise when the requires graph has a cycle among activated suites."""
    graph = {name: set(index[name].requires).intersection(names) for name in names}
    visiting: set[str] = set()
    visited: set[str] = set()

    def dfs(node: str) -> None:
        if node in visiting:
            raise ValueError(f"requires cycle detected involving Suite {node!r}")
        if node in visited:
            return
        visiting.add(node)
        for dep in graph.get(node, ()):
            if dep in names:
                dfs(dep)
        visiting.remove(node)
        visited.add(node)

    for name in names:
        dfs(name)
