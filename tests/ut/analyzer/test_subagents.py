from __future__ import annotations

import json
import zipfile
from argparse import Namespace
from pathlib import Path

from scripts.analyzer import cli as analyzer_cli
from scripts.analyzer import generate_report
from scripts.analyzer.cli import (
    _AnalysisJob,
    _analyze_one,
    _build_batch_jobs,
    _build_log_file_index,
    _finalize_parent_reports,
    _generate_subagent_reports,
    _list_sessions_in_user_dir,
    _maybe_zip_report,
    _parse_bool,
    _resolve_output_path,
    _resolve_user_root,
    _worker_count,
)
from scripts.analyzer.performance import PerformanceDataset
from scripts.analyzer.report import HTMLReportGenerator
from scripts.analyzer.subagents import SubagentAnalyzer
from scripts.analyzer.time import TimeAnalyzer
from scripts.analyzer.token import TokenAnalyzer


def _write_llm_performance(path: Path, total_tokens: int, sub_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "kind": "llm",
        "name": f"model-{sub_id}",
        "run_id": "0",
        "sub_id": sub_id,
        "pid": 99,
        "elapsed_ms": 1000,
        "success": True,
        "started_at": "2026-07-11 00:00:00.000",
        "ended_at": "2026-07-11 00:00:01.000",
        "extra": {"input_tokens": total_tokens - 1, "output_tokens": 1, "total_tokens": total_tokens},
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_resolve_output_path_treats_suffixless_output_as_bundle_dir(tmp_path) -> None:
    session_root = tmp_path / "session"
    bundle_root = tmp_path / "bundle"
    session_root.mkdir()

    assert _resolve_output_path(session_root, None, None) == session_root / "report.html"
    assert _resolve_output_path(session_root, str(bundle_root), None) == (bundle_root / "index.html").resolve()
    assert _resolve_output_path(session_root, None, bundle_root) == (bundle_root / "index.html").resolve()
    assert (
        _resolve_output_path(session_root, str(tmp_path / "custom.html"), None) == (tmp_path / "custom.html").resolve()
    )


def test_override_false_skips_existing_output(tmp_path) -> None:
    session_root = tmp_path / "session"
    output = tmp_path / "report.html"
    session_root.mkdir()
    output.write_text("existing", encoding="utf-8")
    args = Namespace(output=str(output), log_dir=None, logs_only=False, trajectory_only=False, override=False)

    assert _analyze_one(session_root, args) == 0
    assert output.read_text(encoding="utf-8") == "existing"


def test_generate_subagent_reports_records_nonzero_exit_code(monkeypatch, tmp_path) -> None:
    subagent_root = tmp_path / "subagent"
    subagent_root.mkdir()
    report_path = tmp_path / "reports" / "index.html"
    item = {"path": str(subagent_root), "report_path": str(report_path)}
    results = {"subagents": {"subagents": [item], "summary": {}}}
    args = Namespace()

    def _fake_analyze_one(*unused_args: object, **unused_kwargs: object) -> int:
        return 2

    monkeypatch.setattr(analyzer_cli, "_analyze_one", _fake_analyze_one)

    _generate_subagent_reports(results, args)

    assert item.get("report_exists") is False
    assert item.get("report_error") == "Analyzer exited with status 2"


def test_generate_subagent_reports_records_exception(monkeypatch, tmp_path) -> None:
    subagent_root = tmp_path / "subagent"
    subagent_root.mkdir()
    report_path = tmp_path / "reports" / "index.html"
    item = {"path": str(subagent_root), "report_path": str(report_path)}
    results = {"subagents": {"subagents": [item], "summary": {}}}
    args = Namespace()

    def _fake_analyze_one(*unused_args: object, **unused_kwargs: object) -> int:
        raise RuntimeError("analysis failed")

    monkeypatch.setattr(analyzer_cli, "_analyze_one", _fake_analyze_one)

    _generate_subagent_reports(results, args)

    assert item.get("report_exists") is False
    assert item.get("report_error") == "RuntimeError: analysis failed"


def test_worker_count_defaults_to_cpu_bounded_session_count() -> None:
    assert _worker_count(None, 1) == 1
    assert _worker_count(99, 2) == 2
    assert _worker_count(0, 3) == 1


def test_parse_bool_accepts_false_literal() -> None:
    assert _parse_bool("false") is False
    assert _parse_bool("true") is True


def test_zip_report_includes_report_data_and_subagent_reports(tmp_path) -> None:
    bundle = tmp_path / "report_bundle"
    child = bundle / "subagent_reports" / "subagent_1"
    child.mkdir(parents=True)
    (bundle / "index.html").write_text("<html></html>", encoding="utf-8")
    (bundle / "report-data.json").write_text("{}", encoding="utf-8")
    (child / "index.html").write_text("<html></html>", encoding="utf-8")
    (child / "report-data.json").write_text("{}", encoding="utf-8")

    zip_path = _maybe_zip_report(
        bundle / "index.html",
        {"subagents": {"subagent_count": 1, "subagents": [{"report_path": str(child / "index.html")}]}},
        generate_subagent_reports=True,
    )

    assert zip_path == bundle.with_suffix(".zip")
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert "report_bundle/index.html" in names
    assert "report_bundle/report-data.json" in names
    assert "report_bundle/subagent_reports/subagent_1/index.html" in names
    assert "report_bundle/subagent_reports/subagent_1/report-data.json" in names


def test_list_sessions_in_user_dir_returns_context_dirs(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    session = user_root / "session_a"
    subagent = user_root / "subagent_session_a_123456"
    ignored = user_root / "not_a_session"
    (session / ".context").mkdir(parents=True)
    (subagent / ".context").mkdir(parents=True)
    ignored.mkdir()

    assert _list_sessions_in_user_dir(user_root) == [session]


def test_resolve_user_root_treats_bare_user_as_dataagent_user(tmp_path) -> None:
    assert _resolve_user_root("anonymous", data_home=tmp_path) == (tmp_path / "anonymous").resolve()
    assert _resolve_user_root(str(tmp_path / "explicit"), data_home=tmp_path) == (tmp_path / "explicit").resolve()


def test_main_user_id_without_all_runs_batch_analysis(monkeypatch, tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    session = user_root / "session_a"
    (session / ".context").mkdir(parents=True)
    captured: dict[str, object] = {}

    def fake_analyze_many(sessions, args) -> int:
        captured["sessions"] = sessions
        captured["args"] = args
        return 0

    def fake_resolve_user_root(user) -> Path:
        return user_root

    monkeypatch.setattr(analyzer_cli, "_resolve_user_root", fake_resolve_user_root)
    monkeypatch.setattr(analyzer_cli, "_analyze_many", fake_analyze_many)

    result = analyzer_cli.main(["--user-id", "anonymous", "--workers", "1"])
    args = captured.get("args")

    assert result == 0
    assert captured.get("sessions") == [session]
    assert isinstance(args, Namespace)
    assert args.user_id == "anonymous"


def test_build_batch_jobs_schedules_subagents_independently(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    parent = user_root / "20260604_121316_parent-token"
    child = user_root / "subagent_20260604_121316_parent-token_123456"
    for session in (parent, child):
        (session / ".context").mkdir(parents=True)
        (session / ".context" / "Run0_Sub0.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    args = Namespace(output=str(tmp_path / "bundle"), log_dir=None, logs_only=False, trajectory_only=False)

    jobs, parent_jobs = _build_batch_jobs([parent], args)

    assert [job.session_path for job in parent_jobs] == [parent]
    assert [job.session_path for job in jobs] == [parent, child]
    assert jobs[0].generate_subagent_reports is False
    assert jobs[1].generate_subagent_reports is False
    assert (
        jobs[1].output_override
        == (tmp_path / "bundle" / parent.name / "subagent_reports" / child.name / "index.html").resolve()
    )


def test_build_batch_jobs_schedules_workspace_and_inline_subagents(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    workspace_parent = user_root / "workspace-parent"
    workspace_child = workspace_parent / "subagents" / "child-a"
    inline_parent = user_root / "inline-parent"
    (workspace_parent / ".context").mkdir(parents=True)
    (workspace_child / ".context").mkdir(parents=True)
    (workspace_child / ".context" / "Run0_Sub7.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    (inline_parent / ".context").mkdir(parents=True)
    for sub_id in ("0", "8", "9"):
        (inline_parent / ".context" / f"Run0_Sub{sub_id}.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    args = Namespace(output=str(tmp_path / "bundle"), log_dir=None, logs_only=False, trajectory_only=False)

    jobs, _ = _build_batch_jobs([workspace_parent, inline_parent], args)

    child_jobs = [job for job in jobs if job.analysis_scope is not None]
    assert len(child_jobs) == 3
    scopes = [job.analysis_scope or {} for job in child_jobs]
    assert {scope.get("kind") for scope in scopes} == {"workspace_subagent", "inline_shared_workspace"}
    inline_jobs = [job for job in child_jobs if (job.analysis_scope or {}).get("kind") == "inline_shared_workspace"]
    assert {job.session_path for job in inline_jobs} == {inline_parent.resolve()}
    assert {job.output_override.parent.name for job in inline_jobs if job.output_override is not None} == {
        "sub-8",
        "sub-9",
    }


def test_build_log_file_index_matches_full_session_ids_in_parallel(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    parent = user_root / "20260604_121316_e93a436f-6169-487b-acb1-30749d03edc5"
    child = user_root / f"subagent_{parent.name}_396212"
    for session in (parent, child):
        (session / ".context").mkdir(parents=True)
    log_root = user_root / "logs"
    log_root.mkdir()
    parent_log = log_root / "dataagent.log"
    child_log = log_root / f"{child.name}_396212.log"
    parent_log.write_text(f"session_id: {parent.name}\n", encoding="utf-8")
    child_log.write_text(f"subagent started with session {child.name}\n", encoding="utf-8")

    index = _build_log_file_index([parent, child], str(log_root), workers=2)

    assert index.get(parent.name) == [parent_log]
    assert index.get(child.name) == [child_log]


def test_finalize_parent_reports_zips_in_parallel(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    jobs: list[_AnalysisJob] = []
    for idx in range(2):
        parent = user_root / f"20260604_12131{idx}_parent-token-{idx}"
        child = user_root / f"subagent_{parent.name}_12345{idx}"
        output = tmp_path / "bundle" / parent.name / "index.html"
        child_report = output.parent / "subagent_reports" / child.name / "index.html"
        child_report.parent.mkdir(parents=True)
        parent.mkdir(parents=True)
        child.mkdir(parents=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        child_payload = {
            "results": {
                "trajectory": {"turn_count": 1, "total_rounds": 1},
                "logs": {"session_start_ts": 1, "session_end_ts": 3, "session_duration_seconds": 2},
                "token": {"overview": {"total_tokens": 100}},
            }
        }
        child_report.with_name("report-data.json").write_text(json.dumps(child_payload), encoding="utf-8")
        child_report.write_text("<html></html>", encoding="utf-8")
        parent_payload = {
            "user_id": "anonymous",
            "session_id": parent.name,
            "results": {
                "subagents": {
                    "subagent_count": 1,
                    "summary": {},
                    "subagents": [
                        {
                            "session_id": child.name,
                            "sub_id": f"12345{idx}",
                            "display_name": "worker",
                            "report_path": str(child_report),
                            "report_href": f"subagent_reports/{child.name}/index.html",
                        }
                    ],
                }
            },
        }
        output.with_name("report-data.json").write_text(json.dumps(parent_payload), encoding="utf-8")
        output.write_text("<html></html>", encoding="utf-8")
        jobs.append(_AnalysisJob(session_path=parent, output_override=output, generate_subagent_reports=False))
    args = Namespace(output=None, log_dir=None, logs_only=False, trajectory_only=False, override=True)

    _finalize_parent_reports(jobs, args, workers=2)

    for job in jobs:
        output = job.output_override
        assert output is not None
        assert output.with_name("report-data.json").is_file()
        assert output.parent.with_suffix(".zip").is_file()


def test_finalize_parent_reports_override_false_backfills_missing_zip(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    parent = user_root / "20260604_121316_parent-token"
    child = user_root / "subagent_20260604_121316_parent-token_123456"
    output = tmp_path / "report.html"
    child_report = tmp_path / "subagent_reports" / child.name / "report.html"
    child_report.parent.mkdir(parents=True)
    parent.mkdir(parents=True)
    child.mkdir(parents=True)
    child_report.write_text("<html>child</html>", encoding="utf-8")
    child_report.with_name("report-data.json").write_text("{}", encoding="utf-8")
    output.write_text("existing parent", encoding="utf-8")
    output.with_name("report-data.json").write_text(
        json.dumps(
            {
                "user_id": "anonymous",
                "session_id": parent.name,
                "results": {
                    "subagents": {
                        "subagent_count": 1,
                        "summary": {},
                        "subagents": [{"session_id": child.name, "report_path": str(child_report)}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    args = Namespace(output=None, log_dir=None, logs_only=False, trajectory_only=False, override=False)

    _finalize_parent_reports([_AnalysisJob(parent, output, False)], args, workers=1)

    assert output.read_text(encoding="utf-8") == "existing parent"
    assert output.with_suffix(".zip").is_file()


def test_generate_report_api_writes_subagent_reports_and_zip(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    parent = user_root / "20260604_121316_parent-token"
    child = user_root / "subagent_20260604_121316_parent-token_123456"
    output = tmp_path / "report_bundle"
    for session in (parent, child):
        (session / ".context").mkdir(parents=True)
        (session / ".context" / "Run0_Sub0.json").write_text(
            json.dumps({"nodes": [{"id": "Query(q0)", "node_type": "Query", "run_id": 0, "query": "hello"}]}),
            encoding="utf-8",
        )

    generate_report(parent, output=output, analyzers=["trajectory", "subagents"])

    assert (output / "index.html").is_file()
    assert (output / "report-data.json").is_file()
    assert (output / "subagent_reports" / child.name / "index.html").is_file()
    assert (output / "subagent_reports" / child.name / "report-data.json").is_file()
    assert output.with_suffix(".zip").is_file()


def test_generate_report_api_override_false_returns_existing_output(tmp_path) -> None:
    parent = tmp_path / "anonymous" / "20260604_121316_parent-token"
    output = tmp_path / "report_bundle"
    parent.mkdir(parents=True)
    output.mkdir()
    (output / "index.html").write_text("existing", encoding="utf-8")

    html = generate_report(parent, output=output, override=False)

    assert html == "existing"
    assert (output / "index.html").read_text(encoding="utf-8") == "existing"


def test_subagent_analyzer_discovers_sibling_session_by_full_parent_session_id(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    parent = user_root / "20260604_121316_e93a436f-6169-487b-acb1-30749d03edc5"
    child = user_root / "subagent_20260604_121316_e93a436f-6169-487b-acb1-30749d03edc5_396212"
    parent.mkdir(parents=True)
    (child / ".context").mkdir(parents=True)
    (child / ".context" / "Run0_Sub0.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    (child / "report.html").write_text("<html></html>", encoding="utf-8")

    result = SubagentAnalyzer().analyze(parent)

    assert result.get("subagent_count") == 1
    item = result.get("subagents", [])[0]
    assert item.get("sub_id") == "396212"
    assert item.get("session_id") == child.name
    assert item.get("context_count") == 1
    assert item.get("report_exists") is True
    assert item.get("match_mode") == "sibling_session_id"


def test_subagent_analyzer_discovers_parent_session_without_timestamp_prefix(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    parent = user_root / "plain-session-id"
    child = user_root / "subagent_plain-session-id_123456"
    parent.mkdir(parents=True)
    (child / ".context").mkdir(parents=True)

    result = SubagentAnalyzer().analyze(parent)

    assert result.get("subagent_count") == 1
    assert result.get("subagents", [])[0].get("sub_id") == "123456"


def test_subagent_analyzer_matches_logs_at_file_level(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    parent = user_root / "20260604_121316_parent-token"
    child = user_root / "subagent_20260604_121316_parent-token_123456"
    parent.mkdir(parents=True)
    (child / ".context").mkdir(parents=True)
    (child / ".context" / "Run0_Sub0.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    log_root = user_root / "logs"
    log_root.mkdir()
    log_file = log_root / "dataagent.log"
    log_file.write_text(f"started session {child.name}\nERROR unrelated line without session id\n", encoding="utf-8")

    result = SubagentAnalyzer().analyze(parent)

    item = result.get("subagents", [])[0]
    assert item.get("log_count") == 1
    assert item.get("log_files") == [str(log_file.resolve())]


def test_subagent_analyzer_extracts_display_name_from_log_config(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    parent = user_root / "20260604_121316_parent-token"
    child = user_root / "subagent_20260604_121316_parent-token_123456"
    parent.mkdir(parents=True)
    (child / ".context").mkdir(parents=True)
    (child / ".context" / "Run0_Sub0.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "node_type": "Query",
                        "query": "retrieve business documents",
                        "id": "Query(query00000)",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    log_root = user_root / "logs"
    log_root.mkdir()
    log_file = log_root / f"{child.name}_123456.log"
    log_file.write_text(
        "2026-06-04 12:13:16.000 | TRACE    | subagent | module:reload:1 | "
        "Loaded default configuration file: /tmp/flex_default_configs.yaml\n"
        "2026-06-04 12:13:17.000 | TRACE    | subagent | module:reload:2 | "
        "Loaded configuration file: /tmp/document_recall_sub_agent.yaml\n",
        encoding="utf-8",
    )

    result = SubagentAnalyzer().analyze(parent)

    item = result.get("subagents", [])[0]
    assert item.get("display_name") == "document_recall_sub_agent"
    assert item.get("config_name") == "document_recall_sub_agent"
    assert item.get("config_path") == "/tmp/document_recall_sub_agent.yaml"
    assert item.get("last_query") == "retrieve business documents"


def test_subagent_analyzer_uses_relative_bundle_report_links(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    parent = user_root / "20260604_121316_parent-token"
    child = user_root / "subagent_20260604_121316_parent-token_123456"
    bundle = tmp_path / "bundle"
    parent.mkdir(parents=True)
    (child / ".context").mkdir(parents=True)
    (child / ".context" / "Run0_Sub0.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")

    result = SubagentAnalyzer().analyze(
        parent,
        report_name="index.html",
        report_root=bundle / "subagent_reports",
        parent_report_dir=bundle,
    )

    item = result.get("subagents", [])[0]
    assert item.get("report_path") == str((bundle / "subagent_reports" / child.name / "index.html").resolve())
    assert item.get("report_href") == f"subagent_reports/{child.name}/index.html"


def test_subagent_analyzer_enriches_metrics_from_report_data(tmp_path) -> None:
    child = tmp_path / "subagent_20260604_121316_parent-token_123456"
    report_path = child / "index.html"
    child.mkdir()
    (child / "report-data.json").write_text(
        json.dumps(
            {
                "results": {
                    "trajectory": {"turn_count": 5, "total_rounds": 2},
                    "logs": {
                        "session_start_ts": 1780550000.0,
                        "session_end_ts": 1780550065.0,
                        "session_duration_seconds": 65.0,
                    },
                    "token": {"overview": {"total_tokens": 12345}},
                }
            }
        ),
        encoding="utf-8",
    )
    item = {"session_id": child.name, "report_path": str(report_path)}

    SubagentAnalyzer.enrich_item_from_report(item)

    assert item.get("turn_count") == 5
    assert item.get("total_rounds") == 2
    assert item.get("duration") == "1.08 min"
    assert item.get("total_tokens") == 12345
    assert item.get("total_tokens_display") == "12,345"


def test_subagent_analyzer_ignores_unrelated_sibling_sessions(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    parent = user_root / "20260604_121316_parent-token"
    unrelated = user_root / "subagent_20260604_121316_other-token_123456"
    parent.mkdir(parents=True)
    (unrelated / ".context").mkdir(parents=True)

    result = SubagentAnalyzer().analyze(parent)

    assert result.get("subagent_count") == 0


def test_subagent_analyzer_prefers_workspace_subagents_directory(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    parent = user_root / "20260711_101500_parent-token"
    child = parent / "subagents" / "research-worker"
    (parent / ".context").mkdir(parents=True)
    (parent / ".context" / "Run0_Sub0.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    (child / ".context").mkdir(parents=True)
    (child / ".context" / "Run0_Sub42.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "node_type": "Query",
                        "id": "Query(query00000)",
                        "run_id": 0,
                        "query": "research the schema",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    log_root = user_root / "logs"
    log_root.mkdir()
    child_log = log_root / f"subagent_{parent.name}_42_42.log"
    child_log.write_text(
        "2026-07-11 10:15:01.000 | INFO | subagent | module:load:1 | "
        "Loaded configuration file: /tmp/research_agent.yaml\n",
        encoding="utf-8",
    )

    result = SubagentAnalyzer().analyze(parent)

    assert result.get("subagent_count") == 1
    item = result.get("subagents", [])[0]
    assert item.get("session_id") == "research-worker"
    assert item.get("sub_id") == "42"
    assert item.get("match_mode") == "workspace_subagents_dir"
    assert item.get("display_name") == "research_agent"
    assert item.get("last_query") == "research the schema"
    assert item.get("log_files") == [str(child_log.resolve())]
    scope = item.get("analysis_scope", {})
    assert scope.get("kind") == "workspace_subagent"


def test_inline_subagent_report_scopes_context_performance_and_logs(tmp_path) -> None:
    user_root = tmp_path / "anonymous"
    parent = user_root / "20260711_101500_parent-token"
    context_dir = parent / ".context"
    performance_dir = parent / ".performance"
    context_dir.mkdir(parents=True)
    performance_dir.mkdir()
    (context_dir / "Run0_Sub0.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {"node_type": "Query", "id": "Query(main)", "run_id": 0, "query": "main query"},
                    {"node_type": "Action", "id": "Action(main-call)", "run_id": 0, "action": "main_tool"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (context_dir / "Run0_Sub42.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {"node_type": "Query", "id": "Query(child)", "run_id": 0, "query": "child query"},
                    {"node_type": "Action", "id": "Action(child-call)", "run_id": 0, "action": "child_tool"},
                ]
            }
        ),
        encoding="utf-8",
    )
    performance_file = performance_dir / "0.321.jsonl"
    performance_records = [
        {
            "kind": "llm",
            "name": "child-model",
            "elapsed_ms": 1000,
            "success": True,
            "started_at": "2026-07-11 02:15:01.000",
            "ended_at": "2026-07-11 02:15:02.000",
            "extra": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        },
        {
            "kind": "_flush",
            "metadata": {
                "user_id": "anonymous",
                "session_id": parent.name,
                "run_id": "0",
                "pid": 321,
                "started_at": "2026-07-11 02:15:00.000",
                "ended_at": "2026-07-11 02:15:03.000",
                "e2e_ms": 3000,
            },
            "summary": {"llms": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}},
        },
    ]
    performance_file.write_text(
        "\n".join(json.dumps(record) for record in performance_records) + "\n",
        encoding="utf-8",
    )
    log_root = user_root / "logs"
    log_root.mkdir()
    child_log = log_root / f"subagent_{parent.name}_42_42.log"
    child_log.write_text(
        f"2026-07-11 10:15:00.000 | INFO | subagent | performance:init:1 | "
        f"[perf] enabled, jsonl={performance_file}\n"
        "2026-07-11 10:15:03.000 | WARNING | subagent | worker:run:2 | child warning\n",
        encoding="utf-8",
    )
    output = tmp_path / "bundle"

    generate_report(parent, output=output)

    parent_payload = json.loads((output / "report-data.json").read_text(encoding="utf-8"))
    parent_results = parent_payload.get("results", {})
    parent_trajectory = parent_results.get("trajectory", {})
    assert [row.get("tool") for row in parent_trajectory.get("tool_stats", [])] == ["main_tool"]
    subagents = parent_results.get("subagents", {}).get("subagents", [])
    assert len(subagents) == 1
    assert subagents[0].get("match_mode") == "inline_context"
    assert subagents[0].get("performance_match_mode") == "subagent_log_jsonl_path"

    child_data = output / "subagent_reports" / "sub-42" / "report-data.json"
    child_payload = json.loads(child_data.read_text(encoding="utf-8"))
    child_results = child_payload.get("results", {})
    child_trajectory = child_results.get("trajectory", {})
    child_token = child_results.get("token", {})
    child_logs = child_results.get("logs", {})
    assert [row.get("tool") for row in child_trajectory.get("tool_stats", [])] == ["child_tool"]
    assert child_token.get("overview", {}).get("total_tokens") == 120
    assert child_logs.get("warning_count") == 1
    assert child_logs.get("matched_log_files") == [child_log.name]


def test_inline_subagent_without_proof_does_not_claim_shared_performance(tmp_path) -> None:
    parent = tmp_path / "anonymous" / "plain-session"
    context_dir = parent / ".context"
    performance_dir = parent / ".performance"
    context_dir.mkdir(parents=True)
    performance_dir.mkdir()
    for sub_id in ("0", "9"):
        (context_dir / f"Run0_Sub{sub_id}.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    (performance_dir / "0.99.jsonl").write_text(
        json.dumps(
            {
                "kind": "llm",
                "name": "unknown-owner",
                "elapsed_ms": 1,
                "success": True,
                "started_at": "2026-07-11 00:00:00.000",
                "ended_at": "2026-07-11 00:00:00.001",
                "extra": {"total_tokens": 10},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = SubagentAnalyzer().analyze(parent)

    item = result.get("subagents", [])[0]
    scope = item.get("analysis_scope", {})
    assert scope.get("performance_files") == ()
    assert scope.get("performance_match_mode") == "ambiguous_shared_performance"
    assert "cannot be attributed reliably" in scope.get("performance_error", "")


def test_inline_subagent_matches_new_performance_filename_by_sub_id(tmp_path) -> None:
    parent = tmp_path / "anonymous" / "plain-session"
    context_dir = parent / ".context"
    performance_dir = parent / ".performance"
    context_dir.mkdir(parents=True)
    performance_dir.mkdir()
    (context_dir / "Run0_Sub0.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    (context_dir / "Run0_Sub9.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    (performance_dir / "Run0_Sub0.98.jsonl").write_text("", encoding="utf-8")
    first_performance_file = performance_dir / "Run0_Sub9.99.jsonl"
    first_performance_file.write_text(
        json.dumps(
            {
                "kind": "tool",
                "name": "bash",
                "run_id": "0",
                "sub_id": "9",
                "pid": 99,
                "elapsed_ms": 1,
                "success": True,
                "started_at": "2026-07-11 00:00:00.000",
                "ended_at": "2026-07-11 00:00:00.001",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    second_performance_file = performance_dir / "Run1_Sub9.100.jsonl"
    _write_llm_performance(second_performance_file, 12, "9")

    result = SubagentAnalyzer().analyze(parent)

    scope = result.get("subagents", [])[0].get("analysis_scope", {})
    assert scope.get("performance_files") == (
        str(first_performance_file.resolve()),
        str(second_performance_file.resolve()),
    )
    assert scope.get("performance_match_mode") == "filename_sub_id"


def test_shared_performance_scopes_time_and_token_by_sub_id(tmp_path) -> None:
    """Shared workspace reports must not mix parent and child performance events."""
    parent = tmp_path / "anonymous" / "shared-session"
    context_dir = parent / ".context"
    context_dir.mkdir(parents=True)
    (context_dir / "Run0_Sub0.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    (context_dir / "Run0_Sub9.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    main_file = parent / ".performance" / "Run0_Sub0.98.jsonl"
    child_file = parent / ".performance" / "Run0_Sub9.99.jsonl"
    _write_llm_performance(main_file, 100, "0")
    _write_llm_performance(child_file, 900, "9")
    analyzer = SubagentAnalyzer()

    main_scope = analyzer.resolve_main_scope(parent)
    child_spec = analyzer.discover_job_specs(parent)[0]
    child_scope = analyzer.resolve_scope(parent, child_spec.get("analysis_scope"))
    main_dataset = PerformanceDataset.load(parent, main_scope)
    child_dataset = PerformanceDataset.load(parent, child_scope)
    main_time = TimeAnalyzer().analyze(parent, performance_dataset=main_dataset)
    child_time = TimeAnalyzer().analyze(parent, performance_dataset=child_dataset)
    main_token = TokenAnalyzer().analyze(parent, performance_dataset=main_dataset)
    child_token = TokenAnalyzer().analyze(parent, performance_dataset=child_dataset)

    assert main_time.get("source_files") == [main_file.name]
    assert child_time.get("source_files") == [child_file.name]
    assert main_token.get("overview", {}).get("total_tokens") == 100
    assert child_token.get("overview", {}).get("total_tokens") == 900


def test_main_scope_excludes_nonzero_sub_without_matching_context(tmp_path) -> None:
    """Main time and token data must exclude every explicitly nonzero Sub file."""
    parent = tmp_path / "anonymous" / "partial-shared-session"
    context_dir = parent / ".context"
    context_dir.mkdir(parents=True)
    (context_dir / "Run0_Sub0.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    main_file = parent / ".performance" / "Run0_Sub0.98.jsonl"
    orphan_child_file = parent / ".performance" / "Run0_Sub77.99.jsonl"
    _write_llm_performance(main_file, 100, "0")
    _write_llm_performance(orphan_child_file, 7700, "77")

    main_scope = SubagentAnalyzer().resolve_main_scope(parent)
    dataset = PerformanceDataset.load(parent, main_scope)
    token_result = TokenAnalyzer().analyze(parent, performance_dataset=dataset)

    assert [path.name for path in dataset.files] == [main_file.name]
    assert token_result.get("overview", {}).get("total_tokens") == 100


def test_nonzero_parent_performance_prefers_shared_workspace_layout(tmp_path) -> None:
    """Explicit nonzero Sub files override a stale physical subagents directory."""
    parent = tmp_path / "anonymous" / "mixed-session"
    context_dir = parent / ".context"
    physical_child = parent / "subagents" / "stale-child"
    context_dir.mkdir(parents=True)
    (context_dir / "Run0_Sub0.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    (context_dir / "Run0_Sub7.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    (physical_child / ".context").mkdir(parents=True)
    (physical_child / ".context" / "Run0_Sub42.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    shared_file = parent / ".performance" / "Run0_Sub7.99.jsonl"
    _write_llm_performance(shared_file, 70, "7")

    result = SubagentAnalyzer().analyze(parent)
    specs = SubagentAnalyzer().discover_job_specs(parent)

    assert [item.get("sub_id") for item in result.get("subagents", [])] == ["7"]
    assert result.get("subagents", [])[0].get("match_mode") == "inline_context"
    assert [spec.get("report_key") for spec in specs] == ["sub-7"]
    assert specs[0].get("analysis_scope", {}).get("kind") == "inline_shared_workspace"


def test_independent_subagent_time_and_token_use_child_performance_directory(tmp_path) -> None:
    """Physical subagent reports must read only their own performance directory."""
    parent = tmp_path / "anonymous" / "workspace-session"
    child = parent / "subagents" / "child-42"
    (parent / ".context").mkdir(parents=True)
    (parent / ".context" / "Run0_Sub0.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    (child / ".context").mkdir(parents=True)
    (child / ".context" / "Run0_Sub42.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    main_file = parent / ".performance" / "Run0_Sub0.98.jsonl"
    child_file = child / ".performance" / "Run0_Sub42.99.jsonl"
    _write_llm_performance(main_file, 100, "0")
    _write_llm_performance(child_file, 420, "42")
    analyzer = SubagentAnalyzer()

    spec = analyzer.discover_job_specs(parent)[0]
    child_scope = analyzer.resolve_scope(child, spec.get("analysis_scope"))
    dataset = PerformanceDataset.load(child, child_scope)
    time_result = TimeAnalyzer().analyze(child, performance_dataset=dataset)
    token_result = TokenAnalyzer().analyze(child, performance_dataset=dataset)

    assert time_result.get("source_files") == [child_file.name]
    assert token_result.get("overview", {}).get("total_tokens") == 420


def test_report_renders_subagent_tab_and_report_links(tmp_path) -> None:
    child = tmp_path / "subagent_20260604_121316_parent-token_123456"
    child.mkdir()
    report_path = child / "report.html"
    results = {
        "trajectory": {
            "turn_count": 0,
            "total_rounds": 0,
            "total_actions": 0,
            "failed_actions": [],
            "node_type_counts": {},
            "tool_stats": [],
            "queries": [],
            "timeline": [],
            "runs": [],
            "messages_context": None,
        },
        "subagents": {
            "subagent_count": 1,
            "summary": {"discovered": 1, "with_context": 1, "with_logs": 0, "with_report": 0},
            "subagents": [
                {
                    "sub_id": "123456",
                    "session_id": child.name,
                    "path": str(child),
                    "path_href": child.as_uri(),
                    "report_path": str(report_path),
                    "report_href": report_path.as_uri(),
                    "report_exists": False,
                    "start_time": "2026-06-04 12:13:16",
                    "end_time": "—",
                    "duration": "—",
                    "turn_count": 0,
                    "total_tokens_display": "—",
                    "token_status": "not recorded",
                    "context_count": 1,
                    "context_files": ["Run0_Sub0.json"],
                    "log_count": 0,
                    "log_files": [],
                    "match_mode": "sibling_parent_token",
                    "evidence": "directory name contains parent token",
                }
            ],
        },
        "logs": {
            "error_count": 0,
            "warning_count": 0,
            "errors": [],
            "warnings": [],
            "match_mode": "session_local",
            "matched_log_files": [],
            "session_id_in_logs": True,
        },
    }

    html = HTMLReportGenerator().generate(results, user_id="anonymous", session_id="parent")

    assert 'data-tab="subagents"' in html
    assert 'id="tab-subagents"' in html
    assert "Subagent Sessions" in html
    assert "123456" in html
    assert report_path.as_uri() in html
    assert f'href="{child.as_uri()}"' not in html
