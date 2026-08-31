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
"""Analyzer base classes — extensible framework for offline session analysis."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class AnalyzerSpec:
    """Declarative metadata for one pluggable analyzer.

    The report generator uses this manifest to expose analyzer ordering,
    dependencies, data-source expectations, and empty-state behavior without
    hard-coding those details into the analyzer execution path.
    """

    name: str
    title: str
    order: int
    description: str = ""
    data_sources: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    schema_version: str = "1"
    empty_message: str = "No data was recorded for this analyzer."
    template: Optional[str] = None

    @staticmethod
    def sort_key(spec: AnalyzerSpec) -> tuple[int, str]:
        """Return the stable report ordering key for a specification."""
        return spec.order, spec.name

    def to_dict(self) -> dict[str, Any]:
        """Return the spec as a plain dictionary for JSON serialization."""
        return asdict(self)


class BaseAnalyzer(ABC):
    """Abstract base for all analyzers.

    Each analyzer reads data from a session directory and returns a
    structured dict of results consumed by the HTML report generator.
    """

    name: str = ""
    description: str = ""
    spec: Optional[AnalyzerSpec] = None

    @property
    def analyzer_spec(self) -> AnalyzerSpec:
        """Return the spec metadata, building a default from name/description if none set."""
        return self.spec or AnalyzerSpec(
            name=self.name,
            title=self.name.replace("_", " ").title(),
            order=100,
            description=self.description,
        )

    @abstractmethod
    def analyze(self, session_root: Path, **kwargs: Any) -> dict[str, Any]:
        """Analyze session data and return structured results."""
        ...


class AnalyzerRegistry:
    """Registry for discovering and running analyzers.

    New analyzers can be registered via ``register()`` without modifying
    existing code — the HTML template picks them up by name.
    """

    _analyzers: dict[str, BaseAnalyzer] = {}

    @classmethod
    def register(cls, analyzer: BaseAnalyzer) -> None:
        """Register an analyzer instance by its name."""
        cls._analyzers[analyzer.name] = analyzer

    @classmethod
    def get(cls, name: str) -> Optional[BaseAnalyzer]:
        """Look up a registered analyzer by name."""
        return cls._analyzers.get(name)

    @classmethod
    def all(cls) -> dict[str, BaseAnalyzer]:
        """Return a copy of all registered analyzers."""
        return dict(cls._analyzers)

    @classmethod
    def manifest(cls, names: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """Return registered analyzer metadata ordered for report rendering."""
        selected = set(names) if names is not None else None
        specs: list[AnalyzerSpec] = []
        for name, analyzer in cls._analyzers.items():
            if selected is None or name in selected:
                specs.append(analyzer.analyzer_spec)
        manifest: list[dict[str, Any]] = []
        for spec in sorted(specs, key=AnalyzerSpec.sort_key):
            manifest.append(spec.to_dict())
        return manifest

    @classmethod
    def run_all(cls, session_root: Path, **kwargs: Any) -> dict[str, Any]:
        """Run every registered analyzer and merge their results."""
        combined: dict[str, Any] = {}
        for name, analyzer in cls._analyzers.items():
            try:
                combined[name] = analyzer.analyze(session_root, **kwargs)
            except Exception as exc:
                combined[name] = {"error": str(exc)}
        return combined
