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
"""Resolve metadata for activated Agent configuration Suites."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path


def resolve_activated_suite_root(
    suite_name: str,
    activated_suites: Sequence[Mapping[str, str]] | None,
) -> Path:
    """
    Resolve the absolute root directory for one activated Suite by name.

    Only suites present in ``ConfigManager.activated_suites`` after a successful
    ``reload()`` are visible to this helper.

    Args:
        suite_name: ``name`` from ``suite.yaml`` (e.g. ``ecommerce_suite``).
        activated_suites: ``ConfigManager.activated_suites`` metadata list.

    Returns:
        Resolved absolute Suite root directory.

    Raises:
        ValueError: ``suite_name`` is empty, or the Suite is not activated.
    """
    name = str(suite_name or "").strip()
    if not name:
        raise ValueError("suite_name must be non-empty")

    for entry in activated_suites or ():
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("name") or "").strip() != name:
            continue
        raw_root = entry.get("root")
        if raw_root is None or not str(raw_root).strip():
            break
        return Path(str(raw_root)).expanduser().resolve()

    raise ValueError(f"Suite {name!r} is not activated")
