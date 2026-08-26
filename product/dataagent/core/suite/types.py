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
"""Shared types for Suite discovery and merge."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SuiteRecord:
    """One discovered and activated Suite instance."""

    name: str
    root: Path
    priority: int
    enabled: bool
    requires: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SuiteIndexEntry:
    """Indexed Suite from disk scan (may not be activated)."""

    name: str
    root: Path
    priority: int
    enabled: bool
    requires: tuple[str, ...]
    conflicts: tuple[str, ...]
    meta: dict[str, Any]
