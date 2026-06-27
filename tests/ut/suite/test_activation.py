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
"""Tests for Suite activation rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from dataagent.suite.activation import activate_suites, order_suites_for_merge
from dataagent.suite.types import SuiteIndexEntry


def _entry(
    name: str,
    *,
    root: str = "/tmp",
    priority: int = 0,
    enabled: bool = True,
    requires: tuple[str, ...] = (),
    conflicts: tuple[str, ...] = (),
) -> SuiteIndexEntry:
    """Build a minimal ``SuiteIndexEntry`` for activation tests."""
    return SuiteIndexEntry(
        name=name,
        root=Path(root),
        priority=priority,
        enabled=enabled,
        requires=requires,
        conflicts=conflicts,
        meta={},
    )


def test_activate_requires_closure() -> None:
    index = {
        "base": _entry("base", requires=("dep",)),
        "dep": _entry("dep"),
    }
    activated = activate_suites(suite_config={"include": ["base"]}, index=index)
    assert [item.name for item in activated] == ["base", "dep"]
    assert [item.name for item in order_suites_for_merge(activated)] == ["dep", "base"]


def test_activate_requires_cycle_raises() -> None:
    index = {
        "a": _entry("a", requires=("b",)),
        "b": _entry("b", requires=("a",)),
    }
    with pytest.raises(ValueError, match="requires cycle"):
        activate_suites(suite_config={"include": ["a"]}, index=index)


def test_activate_priority_override_must_be_integer() -> None:
    index = {"demo": _entry("demo")}
    with pytest.raises(ValueError, match="priority_override must be an integer"):
        activate_suites(
            suite_config={"include": [{"name": "demo", "priority_override": True}]},
            index=index,
        )


def test_activate_priority_override_rejects_string() -> None:
    index = {"demo": _entry("demo")}
    with pytest.raises(ValueError, match="priority_override must be an integer"):
        activate_suites(
            suite_config={"include": [{"name": "demo", "priority_override": "5"}]},
            index=index,
        )


def test_activate_conflicts_raises() -> None:
    index = {
        "left": _entry("left", conflicts=("right",)),
        "right": _entry("right"),
    }
    with pytest.raises(ValueError, match="conflicts"):
        activate_suites(suite_config={"include": ["left", "right"]}, index=index)


def test_activate_required_suite_disabled_raises() -> None:
    index = {
        "base": _entry("base", requires=("dep",)),
        "dep": _entry("dep", enabled=False),
    }
    with pytest.raises(ValueError, match="disabled"):
        activate_suites(suite_config={"include": ["base"]}, index=index)


def test_activate_explicit_disabled_suite_is_skipped() -> None:
    index = {"demo": _entry("demo", enabled=False)}
    activated = activate_suites(suite_config={"include": ["demo"]}, index=index)
    assert activated == []


def test_activate_priority_override_sorts_before_higher_declared_priority() -> None:
    """``priority_override`` replaces ``suite.yaml`` priority for merge ordering."""
    index = {
        "low": _entry("low", priority=10),
        "high": _entry("high", priority=100),
    }
    activated = activate_suites(
        suite_config={
            "include": [
                "low",
                {"name": "high", "priority_override": 5},
            ]
        },
        index=index,
    )
    assert [item.name for item in activated] == ["high", "low"]
    assert activated[0].priority == 5
    assert activated[1].priority == 10


def test_activate_same_priority_name_ordering() -> None:
    """Same priority: activation returns suites sorted by ``name`` ascending (a before z)."""
    index = {
        "alpha": _entry("alpha", priority=50),
        "beta": _entry("beta", priority=50),
    }
    activated = activate_suites(suite_config={"include": ["beta", "alpha"]}, index=index)
    assert [item.name for item in activated] == ["alpha", "beta"]


def test_order_suites_for_merge_reverses_within_priority_bucket() -> None:
    """Merge layer order reverses name-asc activation buckets so list output keeps a before z."""
    index = {
        "alpha": _entry("alpha", priority=50),
        "beta": _entry("beta", priority=50),
        "low": _entry("low", priority=10),
    }
    activated = activate_suites(suite_config={"include": ["beta", "alpha", "low"]}, index=index)
    assert [item.name for item in activated] == ["low", "alpha", "beta"]
    for_merge = order_suites_for_merge(activated)
    assert [item.name for item in for_merge] == ["low", "beta", "alpha"]
