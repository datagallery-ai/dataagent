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
"""CLI entry point for the Ferry session analyzer.

Usage::

    python -m scripts.analyzer --session ~/.dataagent/user123/session_abc
    python -m scripts.analyzer --user-id user123
    python -m scripts.analyzer --user-id user123 --session-id session_abc
    python -m scripts.analyzer --user-id user123 --all
    python -m scripts.analyzer --session ... --output report.html
    python -m scripts.analyzer --session ... --logs-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from scripts.analyzer.base import AnalyzerRegistry
from scripts.analyzer.logs import LogAnalyzer
from scripts.analyzer.performance import PerformanceDataset
from scripts.analyzer.report import HTMLReportGenerator
from scripts.analyzer.scope import AnalysisScope
from scripts.analyzer.subagents import SubagentAnalyzer
from scripts.analyzer.time import TimeAnalyzer
from scripts.analyzer.token import TokenAnalyzer
from scripts.analyzer.trajectory import TrajectoryAnalyzer

DEFAULT_ANALYZERS = ["trajectory", "time", "token", "subagents", "logs"]
SUBAGENT_REPORT_DIR = "subagent_reports"


@dataclass
class _AnalysisJob:
    """One independently executable session report job."""

    session_path: Path
    output_override: Optional[Path]
    generate_subagent_reports: bool = False
    analysis_scope: Optional[dict[str, Any]] = None


def _register_defaults() -> None:
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


def _resolve_session_path(session: Optional[str], user_id: Optional[str], session_id: Optional[str]) -> Optional[Path]:
    """Resolve session directory from CLI args, or return None if unresolved."""
    if session:
        p = Path(session).expanduser().resolve()
        if p.is_dir():
            return p
        logger.warning(f"Session directory not found: {p}", file=sys.stderr)
        return None

    if user_id and session_id:
        p = Path.home() / ".dataagent" / user_id / session_id
        if p.is_dir():
            return p.resolve()
        logger.warning(f"Session directory not found: {p}", file=sys.stderr)
        return None

    return None


def _list_sessions(user_id: str) -> list[Path]:
    """List all session directories for a user."""
    return _list_sessions_in_user_dir(_resolve_user_root(user_id))


def _resolve_user_root(user: str, data_home: Optional[Path] = None) -> Path:
    """Resolve a user id to ~/.dataagent/<user>, while still accepting explicit paths."""
    value = str(user).strip()
    path = Path(value).expanduser()
    if path.is_absolute() or value.startswith("~") or "/" in value or "\\" in value:
        return path.resolve()
    return ((data_home or (Path.home() / ".dataagent")) / value).resolve()


def _list_sessions_in_user_dir(user_root: Path) -> list[Path]:
    """List all session directories under a user data directory."""
    if not user_root.is_dir():
        return []
    sessions: list[Path] = []
    for entry in sorted(user_root.iterdir()):
        if entry.name.startswith("subagent_"):
            continue
        if entry.is_dir() and (entry / ".context").is_dir():
            sessions.append(entry)
    return sessions


def _parse_bool(value: str) -> bool:
    """Parse a CLI boolean value."""
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "t", "yes", "y"}:
        return True
    if normalized in {"0", "false", "f", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def main(argv: Optional[list[str]] = None) -> int:
    """Run the Ferry Session Analyzer CLI."""
    _register_defaults()

    parser = argparse.ArgumentParser(
        description="Ferry Session Analyzer — offline trajectory & log analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--session", "-s", help="Path to session directory")
    parser.add_argument("--user", help="DataAgent user id under ~/.dataagent, or an explicit user directory path")
    parser.add_argument("--user-id", "-u", help="DataAgent user id; alone or with --all analyzes all sessions")
    parser.add_argument("--session-id", "-i", help="DataAgent session id")
    parser.add_argument("--all", "-a", action="store_true", help="Analyze all sessions for the user")
    parser.add_argument("--output", "-o", help="Output HTML file or bundle directory (default: <session>/report.html)")
    parser.add_argument("--log-dir", help="Path to log directory (default: auto-detect data home logs)")
    parser.add_argument("--logs-only", action="store_true", help="Only run log analysis")
    parser.add_argument("--trajectory-only", action="store_true", help="Only run trajectory analysis")
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel workers for --user/--all (default: CPU count)",
    )
    parser.add_argument("--override", type=_parse_bool, default=True, help="Overwrite existing reports: true/false")

    args = parser.parse_args(argv)

    if args.user:
        user_root = _resolve_user_root(args.user)
        sessions = _list_sessions_in_user_dir(user_root)
        if not sessions:
            logger.error(f"No sessions with .context/ found under user directory: {user_root}")
            return 1
        logger.info(f"Found {len(sessions)} session(s) under user directory: {user_root}")
        return _analyze_many(sessions, args)

    if args.user_id and (args.all or not args.session_id):
        sessions = _list_sessions(args.user_id)
        if not sessions:
            logger.error(f"No sessions with .context/ found for user '{args.user_id}'")
            return 1
        logger.info(f"Found {len(sessions)} session(s) for user '{args.user_id}'")
        return _analyze_many(sessions, args)

    session_path = _resolve_session_path(args.session, args.user_id, args.session_id)
    if session_path is None:
        parser.print_help()
        return 1

    return _analyze_one(session_path, args)


def _analyze_many(sessions: list[Path], args: argparse.Namespace) -> int:
    """Analyze many sessions, writing each session to its own bundle when --output is a directory."""
    jobs, parent_jobs = _build_batch_jobs(sessions, args)
    workers = _worker_count(args.workers, len(jobs))
    logger.info(f"Analyzing {len(jobs)} report job(s) from {len(sessions)} parent session(s) with {workers} worker(s)")
    job_paths: list[Path] = []
    for job in jobs:
        job_paths.append(job.session_path)
    log_file_index = _build_log_file_index(job_paths, args.log_dir, workers)
    if workers == 1:
        failures = 0
        for job in jobs:
            failures += int(
                bool(
                    _analyze_one(
                        job.session_path,
                        args,
                        output_override=job.output_override,
                        generate_subagent_reports=job.generate_subagent_reports,
                        log_file_index=log_file_index,
                        analysis_scope=job.analysis_scope,
                    )
                )
            )
        _finalize_parent_reports(parent_jobs, args, workers)
        return 1 if failures else 0

    failures = 0
    payload = _worker_args(args)
    index_payload: dict[str, list[str]] = {}
    for key, paths in log_file_index.items():
        values: list[str] = []
        for path in paths:
            values.append(str(path))
        index_payload[key] = values
    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                _analyze_one_worker,
                str(session_path),
                payload,
                str(output_override) if output_override is not None else None,
                job.generate_subagent_reports,
                index_payload,
                job.analysis_scope,
            ): session_path
            for job in jobs
            for session_path, output_override in [(job.session_path, job.output_override)]
        }
        for future in as_completed(future_map):
            session_path = future_map.get(future)
            session_name, code, error = future.result()
            if code:
                failures += 1
                logger.error(f"Failed analyzing {session_name or session_path}: {error}")
            else:
                logger.info(f"Finished analyzing {session_name or session_path}")
    _finalize_parent_reports(parent_jobs, args, workers)
    return 1 if failures else 0


def _build_batch_jobs(sessions: list[Path], args: argparse.Namespace) -> tuple[list[_AnalysisJob], list[_AnalysisJob]]:
    """Build parent and subagent jobs for a batch run."""
    selected = _selected_analyzer_names(args)
    include_subagents = "subagents" in selected
    jobs: list[_AnalysisJob] = []
    parent_jobs: list[_AnalysisJob] = []
    seen: set[tuple[Path, str]] = set()
    subagent_analyzer = SubagentAnalyzer()
    for session_path in sessions:
        output_override = _output_override_for_session(session_path, args)
        parent_job = _AnalysisJob(session_path=session_path, output_override=output_override)
        jobs.append(parent_job)
        parent_jobs.append(parent_job)
        seen.add((session_path.resolve(), ""))
        if not include_subagents:
            continue
        output = _resolve_output_path(session_path, args.output, output_override)
        for spec in subagent_analyzer.discover_job_specs(session_path):
            subagent_path = Path(str(spec.get("session_path", ""))).resolve()
            analysis_scope = spec.get("analysis_scope")
            scope = AnalysisScope.from_value(analysis_scope)
            scope_session_id = scope.session_id if scope else subagent_path.name
            seen_key = (subagent_path, scope_session_id)
            if seen_key in seen or not subagent_path.is_dir():
                continue
            seen.add(seen_key)
            report_key = str(spec.get("report_key", subagent_path.name))
            report_path = output.parent / SUBAGENT_REPORT_DIR / report_key / output.name
            jobs.append(
                _AnalysisJob(
                    session_path=subagent_path,
                    output_override=report_path,
                    generate_subagent_reports=False,
                    analysis_scope=analysis_scope if isinstance(analysis_scope, dict) else None,
                )
            )
    return jobs, parent_jobs


def _selected_analyzer_names(args: argparse.Namespace) -> list[str]:
    """Return analyzer names selected by CLI arguments."""
    requested_analyzers = getattr(args, "analyzers", None)
    if requested_analyzers:
        return list(requested_analyzers)
    if args.logs_only:
        return ["logs"]
    if args.trajectory_only:
        return ["trajectory"]
    return DEFAULT_ANALYZERS


def _finalize_parent_reports(parent_jobs: list[_AnalysisJob], args: argparse.Namespace, workers: int = 1) -> None:
    """Refresh parent reports after child subagent reports have been generated."""
    if "subagents" not in _selected_analyzer_names(args):
        return
    tasks: list[tuple[Path, Path, bool]] = []
    for job in parent_jobs:
        output = _resolve_output_path(job.session_path, args.output, job.output_override)
        if not getattr(args, "override", True) and output.is_file():
            zip_path = _expected_report_zip_path(output)
            if not zip_path.is_file():
                tasks.append((job.session_path, output, True))
            continue
        tasks.append((job.session_path, output, False))
    if not tasks:
        return
    finalize_workers = min(max(int(workers or 1), 1), len(tasks))
    if finalize_workers == 1:
        for session_path, output, zip_only in tasks:
            zip_path = _finalize_parent_report(session_path, output, zip_only=zip_only)
            if zip_path is not None:
                logger.info(f"Report bundle zipped to: {zip_path}")
        return
    with ProcessPoolExecutor(max_workers=finalize_workers) as executor:
        future_map = {
            executor.submit(_finalize_parent_report_worker, str(session_path), str(output), zip_only): session_path
            for session_path, output, zip_only in tasks
        }
        for future in as_completed(future_map):
            session_name, zip_path, error = future.result()
            if error:
                logger.error(f"Failed finalizing {session_name}: {error}")
            elif zip_path:
                logger.info(f"Report bundle zipped to: {zip_path}")


def _finalize_parent_report_worker(session_path: str, output: str, zip_only: bool = False) -> tuple[str, str, str]:
    """Finalize one parent report in a worker process."""
    try:
        zip_path = _finalize_parent_report(Path(session_path), Path(output), zip_only=zip_only)
        return Path(session_path).name, str(zip_path) if zip_path is not None else "", ""
    except Exception as exc:
        return Path(session_path).name, "", str(exc)


def _finalize_parent_report(session_path: Path, output: Path, zip_only: bool = False) -> Optional[Path]:
    """Reload a parent report payload, enrich subagents from child reports, and rewrite package files."""
    if zip_only:
        return _zip_existing_report_package(output)
    data_path = output.with_name("report-data.json")
    if not data_path.is_file():
        return None
    try:
        payload = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    results = payload.get("results", {})
    if not isinstance(results, dict):
        return None
    subagents = results.get("subagents", {})
    if not isinstance(subagents, dict) or not subagents.get("subagent_count"):
        return None
    items = subagents.get("subagents", [])
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        report_path_value = item.get("report_path")
        item["report_exists"] = Path(str(report_path_value)).is_file() if report_path_value else False
        SubagentAnalyzer.enrich_item_from_report(item)
    SubagentAnalyzer.sort_items(items)
    summary = subagents.get("summary", {})
    if isinstance(summary, dict):
        summary.update(SubagentAnalyzer.summarize_items(items))
    user_id = str(payload.get("user_id") or session_path.parent.name)
    session_id = str(payload.get("session_id") or session_path.name)
    HTMLReportGenerator().generate_file(results, user_id=user_id, session_id=session_id, output=output)
    return _maybe_zip_report(output, results, generate_subagent_reports=True)


def _output_override_for_session(session_path: Path, args: argparse.Namespace) -> Optional[Path]:
    """Return the output override for one session in batch mode."""
    output_base = Path(args.output).expanduser() if args.output else None
    if output_base and output_base.suffix.lower() not in (".html", ".htm"):
        return output_base / session_path.name
    if args.output:
        logger.warning(
            "Warning: --output with .html is a file; reports will overwrite each other. "
            "Use a directory for batch mode.",
            file=sys.stderr,
        )
        return Path(args.output)
    return None


def _worker_args(args: argparse.Namespace) -> dict[str, Any]:
    """Return pickle-friendly CLI args for a worker process."""
    return {
        "output": args.output,
        "log_dir": args.log_dir,
        "logs_only": args.logs_only,
        "trajectory_only": args.trajectory_only,
        "analyzers": getattr(args, "analyzers", None),
        "override": args.override,
    }


def _build_log_file_index(
    sessions: list[Path], log_dir: Optional[str], workers: Optional[int] = None
) -> dict[str, list[Path]]:
    """Build a session id to log file index for batch analysis."""
    if not sessions:
        return {}
    session_ids = sorted({session.name for session in sessions})
    log_files = _candidate_batch_log_files(sessions[0], log_dir)
    if not log_files:
        return {}

    scan_workers = min(max(int(workers or 1), 1), len(log_files))
    results: list[tuple[str, list[str]]] = []
    if scan_workers == 1:
        for log_file in log_files:
            results.append(_scan_log_file_for_sessions(str(log_file), session_ids))
    else:
        with ProcessPoolExecutor(max_workers=scan_workers) as executor:
            futures = []
            for log_file in log_files:
                future = executor.submit(_scan_log_file_for_sessions, str(log_file), session_ids)
                futures.append(future)
            for future in as_completed(futures):
                results.append(future.result())

    index: dict[str, list[Path]] = {session_id: [] for session_id in session_ids}
    for log_file, matched_ids in results:
        path = Path(log_file)
        for session_id in matched_ids:
            index.get(session_id, []).append(path)
    return {key: sorted(set(paths)) for key, paths in index.items() if paths}


def _scan_log_file_for_sessions(log_file: str, session_ids: list[str]) -> tuple[str, list[str]]:
    path = Path(log_file)
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return log_file, []
    evidence = f"{path.name}\n{text}"
    matched_ids: list[str] = []
    for session_id in session_ids:
        if _log_file_matches_session(path, evidence, session_id):
            matched_ids.append(session_id)
    return log_file, matched_ids


def _log_file_matches_session(path: Path, evidence: str, session_id: str) -> bool:
    if session_id.startswith("subagent_"):
        return session_id in path.name
    if path.name.startswith(f"subagent_{session_id}_"):
        return False
    return session_id in evidence


def _candidate_batch_log_files(session_root: Path, log_dir: Optional[str]) -> list[Path]:
    """Return candidate global log files for a batch run."""
    log_dirs = LogAnalyzer.resolve_log_dirs(session_root, log_dir)
    return sorted({log_file for log_root in log_dirs for log_file in log_root.glob("*.log")})


def _worker_count(value: Optional[int], session_count: int) -> int:
    """Return the worker count for batch analysis."""
    if session_count <= 1:
        return 1
    requested = value if value is not None else (os.cpu_count() or 1)
    return max(1, min(int(requested), session_count))


def _analyze_one_worker(
    session_path: str,
    args_payload: dict[str, Any],
    output_override: Optional[str],
    generate_subagent_reports: bool,
    log_file_index: dict[str, list[str]],
    analysis_scope: Optional[dict[str, Any]] = None,
) -> tuple[str, int, str]:
    """Run one session analysis in a worker process."""
    _register_defaults()
    args = argparse.Namespace(**args_payload)
    try:
        path = Path(session_path)
        resolved_log_file_index: dict[str, list[Path]] = {}
        for key, values in log_file_index.items():
            paths: list[Path] = []
            for value in values:
                paths.append(Path(value))
            resolved_log_file_index[key] = paths
        code = _analyze_one(
            path,
            args,
            output_override=Path(output_override) if output_override else None,
            generate_subagent_reports=generate_subagent_reports,
            log_file_index=resolved_log_file_index,
            analysis_scope=analysis_scope,
        )
        return path.name, code, ""
    except Exception as exc:
        return Path(session_path).name, 1, str(exc)


def _analyze_one(
    session_path: Path,
    args: argparse.Namespace,
    output_override: Optional[Path] = None,
    generate_subagent_reports: bool = True,
    log_file_index: Optional[dict[str, list[Path]]] = None,
    analysis_scope: Any = None,
) -> int:
    """Analyze one session and write its report."""
    subagent_analyzer = SubagentAnalyzer()
    scope = subagent_analyzer.resolve_scope(session_path, analysis_scope)
    if scope is None:
        scope = subagent_analyzer.resolve_main_scope(session_path)
    user_id = _user_id_for_session(session_path, scope)
    session_id = scope.session_id if scope and scope.session_id else session_path.name

    logger.info(f"Analyzing session: {session_path}")

    output = _resolve_output_path(session_path, args.output, output_override)
    if not getattr(args, "override", True) and output.is_file():
        logger.info(f"Skipping existing report because override=false: {output}")
        zip_path = _zip_existing_report_package(output) if generate_subagent_reports else None
        if zip_path is not None:
            logger.info(f"Report bundle zipped to: {zip_path}")
        return 0

    # Build analyzer list
    names = _selected_analyzer_names(args)
    if scope and scope.sub_id:
        selected_names: list[str] = []
        for name in names:
            if name != "subagents":
                selected_names.append(name)
        names = selected_names

    results: dict = {}
    performance_dataset = (
        PerformanceDataset.load(session_path, scope) if any(name in {"time", "token"} for name in names) else None
    )
    for name in names:
        analyzer = AnalyzerRegistry.get(name)
        if analyzer is None:
            logger.error(f"  Analyzer '{name}' not registered, skipping")
            continue
        logger.info(f"  Running {name} analyzer...")
        kwargs = {}
        if scope is not None:
            kwargs["analysis_scope"] = scope
        if args.log_dir:
            kwargs["log_dir"] = args.log_dir
        if name == "logs" and log_file_index:
            kwargs["log_file_index"] = log_file_index
        if name in {"time", "token"}:
            kwargs["performance_dataset"] = performance_dataset
        if name == "subagents":
            kwargs["report_name"] = output.name
            kwargs["report_root"] = output.parent / SUBAGENT_REPORT_DIR
            kwargs["parent_report_dir"] = output.parent
        results[name] = analyzer.analyze(session_path, **kwargs)
    results["_manifest"] = AnalyzerRegistry.manifest(names)

    if generate_subagent_reports and "subagents" in names:
        _generate_subagent_reports(results, args)
    gen = HTMLReportGenerator()
    gen.generate_file(results, user_id=user_id, session_id=session_id, output=output)
    logger.info(f"Report written to: {output}")
    zip_path = _maybe_zip_report(output, results, generate_subagent_reports)
    if zip_path is not None:
        logger.info(f"Report bundle zipped to: {zip_path}")
    return 0


def _user_id_for_session(session_path: Path, scope: Optional[AnalysisScope]) -> str:
    """Resolve the DataAgent user id for parent, nested-workspace, and inline sessions."""
    if scope is None or scope.is_inline:
        return session_path.parent.name
    if session_path.parent.name == "subagents":
        return session_path.parent.parent.parent.name
    return session_path.parent.name


def _resolve_output_path(session_path: Path, output_arg: Optional[str], output_override: Optional[Path]) -> Path:
    """Resolve a file output path, treating suffix-less --output as a bundle directory."""
    if output_override:
        output = Path(output_override).expanduser()
        return output.resolve() if output.suffix.lower() in (".html", ".htm") else (output / "index.html").resolve()
    if not output_arg:
        return session_path / "report.html"
    output = Path(output_arg).expanduser()
    if output.suffix.lower() in (".html", ".htm"):
        return output.resolve()
    return (output / "index.html").resolve()


def _maybe_zip_bundle(output: Path, output_arg: Optional[str], output_override: Optional[Path]) -> Optional[Path]:
    if output_override is not None or not output_arg:
        return None
    requested = Path(output_arg).expanduser()
    if requested.suffix.lower() in (".html", ".htm"):
        return None
    return _zip_bundle(output.parent)


def _maybe_zip_report(output: Path, results: dict[str, object], generate_subagent_reports: bool) -> Optional[Path]:
    if not generate_subagent_reports:
        return None
    subagents = results.get("subagents")
    if not isinstance(subagents, dict) or not subagents.get("subagent_count"):
        return None
    return _zip_report_package(output, subagents)


def _zip_existing_report_package(output: Path) -> Optional[Path]:
    """Zip an existing report package without rewriting report HTML or report data."""
    zip_path = _expected_report_zip_path(output)
    if zip_path.is_file():
        return None
    data_path = output.with_name("report-data.json")
    if not data_path.is_file():
        return None
    try:
        payload = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    results = payload.get("results", {})
    if not isinstance(results, dict):
        return None
    subagents = results.get("subagents")
    if not isinstance(subagents, dict) or not subagents.get("subagent_count"):
        return None
    return _zip_report_package(output, subagents)


def _expected_report_zip_path(output: Path) -> Path:
    """Return the zip path produced for a report output path."""
    output = output.resolve()
    if output.name == "index.html":
        return output.parent.with_suffix(".zip")
    return output.with_suffix(".zip")


def _zip_report_package(output: Path, subagents: dict[str, object]) -> Path:
    output = output.resolve()
    bundle_dir = output.parent
    if output.name == "index.html":
        return _zip_bundle(bundle_dir)

    root_name = output.stem
    zip_path = output.with_suffix(".zip")
    files = [output, output.with_name("report-data.json")]
    items = subagents.get("subagents", [])
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            report_path_value = item.get("report_path")
            if not report_path_value:
                continue
            report_path = Path(str(report_path_value))
            files.extend([report_path, report_path.with_name("report-data.json")])

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted({path.resolve() for path in files if path.is_file()}):
            archive.write(path, arcname=Path(root_name) / path.relative_to(bundle_dir))
    return zip_path


def _zip_bundle(bundle_dir: Path) -> Path:
    bundle_dir = bundle_dir.resolve()
    zip_path = bundle_dir.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=Path(bundle_dir.name) / path.relative_to(bundle_dir))
    return zip_path


def _generate_subagent_reports(results: dict, args: argparse.Namespace) -> None:
    """Generate one-level reports for discovered subagent sessions."""
    subagents = results.get("subagents", {})
    items = subagents.get("subagents", [])
    for item in items:
        path_value = item.get("path")
        if not path_value:
            continue
        subagent_path = Path(path_value)
        if not subagent_path.is_dir():
            continue
        report_path = Path(str(item.get("report_path", subagent_path / "report.html")))
        logger.info(f"  Generating subagent report: {subagent_path}")
        try:
            exit_code = _analyze_one(
                subagent_path,
                args,
                output_override=report_path,
                generate_subagent_reports=False,
                analysis_scope=item.get("analysis_scope"),
            )
        except Exception as exc:
            logger.exception(f"  Subagent report failed: {subagent_path}")
            item["report_exists"] = False
            item["report_error"] = f"{type(exc).__name__}: {exc}"
            continue
        if exit_code != 0:
            logger.error(f"  Subagent report exited with status {exit_code}: {subagent_path}")
            item["report_exists"] = False
            item["report_error"] = f"Analyzer exited with status {exit_code}"
            continue
        item["report_exists"] = report_path.is_file()
        if not item.get("report_exists", False):
            item["report_error"] = "Analyzer completed without creating the report"
            continue
        item.pop("report_error", None)
        SubagentAnalyzer.enrich_item_from_report(item)
    SubagentAnalyzer.sort_items(items)
    summary = subagents.get("summary", {})
    summary.update(SubagentAnalyzer.summarize_items(items))


if __name__ == "__main__":
    sys.exit(main())
