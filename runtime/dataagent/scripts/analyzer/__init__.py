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
"""Ferry Session Analyzer — offline trajectory & log analysis with interactive HTML reports.

Usage::

    python -m scripts.analyzer --session ~/.dataagent/user123/session_abc
    python -m scripts.analyzer --user-id user123 --session-id session_abc

API::

    from analyzer import generate_report
    generate_report(session_root=Path("..."), output=Path("report.html"))
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from scripts.analyzer.base import AnalyzerRegistry, AnalyzerSpec, BaseAnalyzer
from scripts.analyzer.logs import LogAnalyzer
from scripts.analyzer.performance import PerformanceDataset
from scripts.analyzer.report import HTMLReportGenerator
from scripts.analyzer.subagents import SubagentAnalyzer
from scripts.analyzer.time import TimeAnalyzer
from scripts.analyzer.token import TokenAnalyzer
from scripts.analyzer.trajectory import TrajectoryAnalyzer

DEFAULT_ANALYZERS = ["trajectory", "time", "token", "subagents", "logs"]


# ── auto-register built-in analyzers ──────────────────────
def _register_builtins() -> None:
    if "trajectory" not in AnalyzerRegistry.all():
        AnalyzerRegistry.register(TrajectoryAnalyzer())
    if "logs" not in AnalyzerRegistry.all():
        AnalyzerRegistry.register(LogAnalyzer())
    if "time" not in AnalyzerRegistry.all():
        AnalyzerRegistry.register(TimeAnalyzer())
    if "token" not in AnalyzerRegistry.all():
        AnalyzerRegistry.register(TokenAnalyzer())
    if "subagents" not in AnalyzerRegistry.all():
        AnalyzerRegistry.register(SubagentAnalyzer())


_register_builtins()


def generate_report(
    session_root: Path,
    *,
    output: Optional[Path] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    log_dir: Optional[str] = None,
    analyzers: Optional[list[str]] = None,
    override: bool = True,
) -> str:
    """Run analyzers on a session directory and return the HTML report string.

    Args:
        session_root: Path to the session directory (contains .context/, .memory/, etc).
        output: If given, write the report to this path. Suffix-less paths are treated as bundle directories.
        user_id: Override user id (auto-detected from session_root if omitted).
        session_id: Override session id (auto-detected from session_root if omitted).
        log_dir: Override log directory (defaults to auto-detecting the session data home).
        analyzers: Which analyzers to run (default: trajectory, time, token, subagents, and logs).
        override: Whether to overwrite an existing output report.

    Returns:
        The full HTML report as a string.
    """
    names = analyzers if analyzers is not None else DEFAULT_ANALYZERS
    uid = user_id or session_root.parent.name
    sid = session_id or session_root.name

    kwargs: dict[str, Any] = {}
    if log_dir:
        kwargs["log_dir"] = log_dir
    output_path = None
    if output is not None:
        resolved_output = output.expanduser()
        output_path = (
            resolved_output.resolve()
            if resolved_output.suffix.lower() in (".html", ".htm")
            else (resolved_output / "index.html").resolve()
        )
        if not override and output_path.is_file():
            return output_path.read_text(encoding="utf-8")

    results: dict[str, Any] = {}
    analysis_scope = SubagentAnalyzer().resolve_main_scope(session_root)
    performance_dataset = (
        PerformanceDataset.load(session_root, analysis_scope)
        if any(name in {"time", "token"} for name in names)
        else None
    )
    for name in names:
        a = AnalyzerRegistry.get(name)
        if a is None:
            results[name] = {"error": f"Unknown analyzer: {name}"}
            continue
        analyzer_kwargs = dict(kwargs)
        if analysis_scope is not None:
            analyzer_kwargs["analysis_scope"] = analysis_scope
        if name in {"time", "token"}:
            analyzer_kwargs["performance_dataset"] = performance_dataset
        if name == "subagents" and output_path is not None:
            analyzer_kwargs["report_name"] = output_path.name
            analyzer_kwargs["report_root"] = output_path.parent / "subagent_reports"
            analyzer_kwargs["parent_report_dir"] = output_path.parent
        results[name] = a.analyze(session_root, **analyzer_kwargs)
    results["_manifest"] = AnalyzerRegistry.manifest(names)

    gen = HTMLReportGenerator()
    if output_path is not None:
        if "subagents" in names:
            from scripts.analyzer.cli import _generate_subagent_reports, _maybe_zip_report

            child_analyzers: list[str] = []
            for name in names:
                if name != "subagents":
                    child_analyzers.append(name)
            args = SimpleNamespace(
                output=None,
                log_dir=log_dir,
                logs_only=False,
                trajectory_only=False,
                analyzers=child_analyzers or ["trajectory"],
                override=override,
            )
            _generate_subagent_reports(results, args)
        gen.generate_file(results, user_id=uid, session_id=sid, output=output_path)
        if "subagents" in names:
            _maybe_zip_report(output_path, results, generate_subagent_reports=True)
        return output_path.read_text(encoding="utf-8")

    return gen.generate(results, user_id=uid, session_id=sid)


__all__ = [
    "AnalyzerRegistry",
    "AnalyzerSpec",
    "BaseAnalyzer",
    "HTMLReportGenerator",
    "LogAnalyzer",
    "SubagentAnalyzer",
    "TimeAnalyzer",
    "TokenAnalyzer",
    "TrajectoryAnalyzer",
    "generate_report",
]
