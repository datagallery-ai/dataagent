#!/usr/bin/env python3
"""Plan one retry for BIRD deferred/agent-error cases and merge its results."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def case_key(item: dict[str, Any]) -> tuple[str, int]:
    if not isinstance(item, dict):
        raise ValueError(f"expected a BIRD result object: {item!r}")
    db_id = item.get("db_id")
    question_id = item.get("question_id")
    if not isinstance(db_id, str) or not db_id or "__q" in db_id:
        raise ValueError(f"invalid db_id in BIRD result: {db_id!r}")
    if isinstance(question_id, bool) or not isinstance(question_id, int) or question_id < 0:
        raise ValueError(f"invalid question_id in BIRD result: {question_id!r}")
    return db_id, question_id


def _display_key(key: tuple[str, int]) -> str:
    return f"{key[0]}__q{key[1]}"


def _index_results(results: list[dict[str, Any]], label: str) -> dict[tuple[str, int], dict[str, Any]]:
    indexed = {}
    for item in results:
        key = case_key(item)
        if key in indexed:
            raise ValueError(f"duplicate {label}: {_display_key(key)}")
        indexed[key] = item
    return indexed


def _summary_paths(root: Path) -> list[Path]:
    """Choose one legacy layout, refusing to silently combine attempts."""
    root = Path(root)
    if root.is_file():
        return [root]
    layouts: list[list[Path]] = []
    for directory in (root, root / "full"):
        direct = directory / "summary.json"
        if direct.is_file():
            layouts.append([direct])
        shard_dirs = sorted(path for path in directory.glob("shard_*") if path.is_dir())
        if shard_dirs:
            missing = [path for path in shard_dirs if not (path / "summary.json").is_file()]
            if missing:
                raise ValueError(f"incomplete shards without summary.json: {missing}")
            layouts.append([path / "summary.json" for path in shard_dirs])
    if not layouts:
        raise ValueError(f"no completed BIRD summaries found under {root}")
    if len(layouts) != 1:
        raise ValueError(f"ambiguous BIRD result layouts under {root}; select a summary file or attempt directory")
    return layouts[0]


def _read_summary(path: Path) -> dict[str, Any]:
    summary = _read_json(path)
    if not isinstance(summary, dict) or not isinstance(summary.get("results"), list):
        raise ValueError(f"missing results list: {path}")
    results = summary["results"]
    for field in ("selected", "completed"):
        value = summary.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value != len(results):
            raise ValueError(f"incomplete BIRD summary ({field} differs from result count): {path}")
    _index_results(results, f"result in {path}")
    return summary


def _validate_result_coverage(root: Path, paths: list[Path], results: list[dict[str, Any]]) -> None:
    """Honor optional selection records without requiring metadata for legacy runs."""
    root = Path(root)
    requested_dir = root.parent if root.is_file() else root
    source_dir = paths[0].parent
    if not root.is_file() and source_dir != requested_dir and source_dir.name.startswith("shard_"):
        source_dir = source_dir.parent
    round_dirs = {requested_dir, source_dir}
    selected_dirs = set(round_dirs)
    if source_dir.name == "full":
        selected_dirs.add(source_dir.parent)
    actual = {case_key(item) for item in results}

    def check(expected_rows: list[dict[str, Any]], path: Path) -> None:
        expected = set(_index_results(expected_rows, f"selected case in {path}"))
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        if missing or unexpected:
            raise ValueError(
                f"BIRD result coverage differs from {path}: "
                f"missing {len(missing)} {list(map(_display_key, missing[:5]))}; "
                f"unexpected {len(unexpected)} {list(map(_display_key, unexpected[:5]))}"
            )

    for directory in sorted(round_dirs):
        path = directory / "workers.json"
        if not path.is_file():
            continue
        manifest = _read_json(path)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("cases"), list):
            raise ValueError(f"invalid worker case selection: {path}")
        selected = []
        for worker_cases in manifest["cases"]:
            if not isinstance(worker_cases, list):
                raise ValueError(f"invalid worker case selection: {path}")
            for identity in worker_cases:
                if not isinstance(identity, list) or len(identity) != 2:
                    raise ValueError(f"invalid worker case identity: {path}")
                selected.append({"db_id": identity[0], "question_id": identity[1]})
        check(selected, path)
    for directory in sorted(selected_dirs):
        path = directory / "selected_questions.json"
        if not path.is_file():
            continue
        selected = _read_json(path)
        if not isinstance(selected, list):
            raise ValueError(f"expected selected question list: {path}")
        check(selected, path)


def load_results(root: Path) -> list[dict[str, Any]]:
    """Read a summary file or one complete direct/full/sharded result layout.

    If a run root also holds derived summaries, pass its ``full`` directory or
    an explicit effective summary file to select the desired attempt.
    """
    paths = _summary_paths(root)
    results = [dict(item) for path in paths for item in _read_summary(path)["results"]]
    _index_results(results, "BIRD result")
    _validate_result_coverage(root, paths, results)
    return results


def _load_full(model_root: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Legacy plan API: select initial results and retain their source shard."""
    model_root = Path(model_root)
    root = model_root / "full" if (model_root / "full").is_dir() else model_root
    summaries: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    paths = _summary_paths(root)
    for path in paths:
        shard = path.parent.name if path.parent.name.startswith("shard_") else "shard_0"
        summary = _read_summary(path)
        summaries[shard] = summary
        results.extend(dict(item, initial_shard=shard) for item in summary["results"])
    _index_results(results, "initial result")
    _validate_result_coverage(root, paths, results)
    return results, summaries, {}


def _load_excluded(path: Path | None) -> tuple[set[int], dict[str, Any]]:
    if path is None:
        return set(), {"path": None, "count": 0, "sha256": None}
    data = path.read_bytes()
    ids: list[int] = []
    saw_heading = False
    declared_total: int | None = None
    for line_number, raw in enumerate(data.decode("utf-8").splitlines(), 1):
        value = raw.strip()
        if not value:
            continue
        if re.fullmatch(r"[0-9]+", value):
            if declared_total is not None:
                raise ValueError(f"question id appears after total at {path}:{line_number}")
            ids.append(int(value))
            continue
        total_match = re.fullmatch(r"(?:总数量|total)\s*[:：]\s*([0-9]+)", value, re.IGNORECASE)
        if total_match and declared_total is None:
            declared_total = int(total_match.group(1))
            continue
        if not saw_heading and not ids and declared_total is None:
            saw_heading = True
            continue
        raise ValueError(f"invalid question id at {path}:{line_number}: {value!r}")
    unique = set(ids)
    if len(unique) != len(ids):
        raise ValueError(f"duplicate question ids in {path}")
    if declared_total is not None and declared_total != len(unique):
        raise ValueError(f"declared total does not match question ids in {path}")
    return unique, {
        "path": str(path),
        "count": len(unique),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def read_question_ids_file(path: Path) -> set[int]:
    """Read numeric IDs, allowing the historical heading and trailing total."""
    return _load_excluded(path)[0]


def summarize_results(results: list[dict[str, Any]], *, selected: int | None = None) -> dict[str, Any]:
    """Keep all selected questions in the accuracy denominator, including failures."""
    _index_results(results, "summary result")
    if selected is None:
        selected = len(results)
    if isinstance(selected, bool) or not isinstance(selected, int) or selected < len(results):
        raise ValueError("selected must be an integer at least as large as the completed result count")
    correct = sum(item.get("correct") is True for item in results)
    return {
        "selected": selected,
        "completed": len(results),
        "predicted": sum(bool(item.get("predicted_sql")) for item in results),
        "correct": correct,
        "agent_error": sum(bool(item.get("error")) and item.get("deferred") is not True for item in results),
        "deferred": sum(item.get("deferred") is True for item in results),
        "failed": sum(item.get("correct") is not True for item in results),
        "accuracy": correct / selected if selected else 0.0,
        "results": results,
    }


def retry_cases(
    results: list[dict[str, Any]],
    *,
    categories: tuple[str, ...] = ("deferred", "agent_error"),
    exclude_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Select original result rows; incorrect answers require explicit opt-in."""
    _index_results(results, "retry candidate")
    unknown = set(categories) - {"deferred", "agent_error", "incorrect"}
    if unknown:
        raise ValueError(f"unknown retry categories: {sorted(unknown)}")
    excluded = {str(value) for value in (exclude_ids or ())}
    selected = []
    for item in results:
        key = case_key(item)
        if str(key[1]) in excluded or _display_key(key) in excluded:
            continue
        if item.get("deferred") is True:
            category = "deferred"
        elif item.get("error"):
            category = "agent_error"
        elif item.get("correct") is not True:
            category = "incorrect"
        else:
            continue
        if category in categories:
            selected.append(dict(item))
    return selected


def merge_retry_results(initial: list[dict[str, Any]], retry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlay one attempt even if it failed, without picking the best-scoring answer."""
    initial_by_key = _index_results(initial, "initial result")
    retry_by_key = _index_results(retry, "retry result")
    unexpected = set(retry_by_key) - set(initial_by_key)
    if unexpected:
        raise ValueError(f"retry contains cases absent from initial results: {sorted(map(_display_key, unexpected))}")
    effective = []
    for key, original in initial_by_key.items():
        if key in retry_by_key:
            item = dict(retry_by_key[key])
            item["effective_attempt"] = "retry"
            item["initial_artifact_dir"] = original.get("artifact_dir")
        else:
            item = dict(original, effective_attempt="initial")
        effective.append(item)
    return effective


def build_plan(
    model_root: Path,
    dev_json: Path,
    retry_dir: Path,
    exclude_question_ids: Path | None = None,
    *,
    expected: int | None = None,
    expected_shards: tuple[str, ...] | None = None,
    categories: tuple[str, ...] = ("deferred", "agent_error"),
) -> dict[str, Any]:
    initial, summaries, _ = _load_full(model_root)
    if expected_shards is not None and set(summaries) != set(expected_shards):
        raise ValueError(f"full shard inventory differs from expected shards: {sorted(summaries)}")
    if expected is not None and len(initial) != expected:
        raise ValueError(f"initial result count {len(initial)} differs from expected {expected}")
    questions = _read_json(dev_json)
    if not isinstance(questions, list):
        raise ValueError(f"expected JSON list: {dev_json}")
    question_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for question in questions:
        key = case_key(question)
        if key in question_by_key:
            raise ValueError(f"duplicate dev question: {_display_key(key)}")
        question_by_key[key] = question
    if exclude_question_ids is not None:
        question_ids = [case_key(item)[1] for item in questions]
        if len(set(question_ids)) != len(question_ids):
            raise ValueError("question_id is not globally unique; an id-only exclude file is ambiguous")
    initial_keys = {case_key(item) for item in initial}
    if not initial_keys <= set(question_by_key):
        raise ValueError("initial run contains questions absent from dev.json")
    excluded, exclude_metadata = _load_excluded(exclude_question_ids)
    unknown_excluded = excluded - {case_key(item)[1] for item in questions}
    if unknown_excluded:
        raise ValueError(f"excluded question ids are absent from dev.json: {sorted(unknown_excluded)}")

    cases: list[dict[str, Any]] = []
    questions_by_shard: dict[str, list[dict[str, Any]]] = {}
    selected = retry_cases(initial, categories=categories, exclude_ids={str(value) for value in excluded})
    for item in selected:
        key = case_key(item)
        if key not in question_by_key:
            raise ValueError(f"full result is absent from dev.json: {_display_key(key)}")
        shard = str(item["initial_shard"])
        cases.append(
            {
                "db_id": item.get("db_id"),
                "question_id": item.get("question_id"),
                "shard": shard,
                "initial_artifact_dir": item.get("artifact_dir"),
                "initial_error": item.get("error"),
                "initial_deferred": item.get("deferred") is True,
            }
        )
        questions_by_shard.setdefault(shard, []).append(question_by_key[key])

    plan = {
        "version": 3,
        "model_root": str(model_root),
        "dev_json": {"path": str(dev_json)},
        "excluded_question_ids": exclude_metadata,
        "expected": len(initial),
        "categories": list(categories),
        "full_shards": {
            shard: {
                "selected": summary.get("selected"),
                "completed": summary.get("completed"),
                "case_timeout_seconds": summary.get("case_timeout_seconds"),
                "semantic_service_url": summary.get("semantic_service_url"),
            }
            for shard, summary in sorted(summaries.items())
        },
        "selected": len(cases),
        "cases": cases,
    }
    plan_path = retry_dir / "plan.json"
    if plan_path.is_file():
        previous = _read_json(plan_path)
        previous_cases = {(case_key(item), item["shard"]) for item in previous.get("cases", [])}
        current_cases = {(case_key(item), item["shard"]) for item in cases}
        if previous_cases != current_cases:
            raise ValueError(f"existing retry selection differs from the requested selection: {plan_path}")
    plans_dir = retry_dir / "plans"
    expected_plan_paths = {plans_dir / f"{shard}_questions.json" for shard in questions_by_shard}
    stale_plan_paths = set(plans_dir.glob("shard_*_questions.json")) - expected_plan_paths
    if stale_plan_paths:
        raise ValueError(f"stale retry question plans found: {sorted(map(str, stale_plan_paths))}")
    _write_json(plan_path, plan)
    for shard, selected_questions in sorted(questions_by_shard.items()):
        _write_json(plans_dir / f"{shard}_questions.json", selected_questions)
    return plan


def _summary(results: list[dict[str, Any]], *, selected: int, base: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**(base or {}), **summarize_results(results, selected=selected)}


def merge_results(model_root: Path, retry_dir: Path, *, write: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge a standalone plan, validating its exact case selection."""
    plan = _read_json(retry_dir / "plan.json")
    initial, full_summaries, _ = _load_full(model_root)
    planned_cases = plan.get("cases")
    if not isinstance(planned_cases, list):
        raise ValueError("retry plan must contain a cases list")
    planned = _index_results(planned_cases, "planned retry case")
    if plan.get("expected") != len(initial) or plan.get("selected") != len(planned):
        raise ValueError("retry plan does not match the current complete initial run")
    initial_by_key = _index_results(initial, "initial result")
    for key, item in planned.items():
        if key not in initial_by_key or item.get("shard") != initial_by_key[key]["initial_shard"]:
            raise ValueError(f"retry selection does not match initial results: {_display_key(key)}")
    if planned:
        retry_results = load_results(retry_dir)
    else:
        # A zero-case plan needs no worker directories, but must not hide stale results.
        has_results = (
            (retry_dir / "summary.json").is_file() or (retry_dir / "full").is_dir() or any(retry_dir.glob("shard_*"))
        )
        retry_results = load_results(retry_dir) if has_results else []
    if {case_key(item) for item in retry_results} != set(planned):
        raise ValueError("retry results do not cover the exact retry plan")
    effective = merge_retry_results(initial, retry_results)
    excluded = plan.get("excluded_question_ids", {})
    retry_summary = _summary(
        retry_results,
        selected=len(planned),
        base={"excluded_question_ids": excluded, "attempt": "retry_once"},
    )
    first_summary = next(iter(full_summaries.values()))
    effective_summary = _summary(
        effective,
        selected=len(initial),
        base={
            "semantic_service_url": first_summary.get("semantic_service_url"),
            "case_timeout_seconds": first_summary.get("case_timeout_seconds"),
            "excluded_question_ids": excluded,
            "retry_selected": len(planned),
        },
    )
    if write:
        _write_json(retry_dir / "retry_summary.json", retry_summary)
        _write_json(retry_dir / "effective_summary.json", effective_summary)
    return effective_summary, retry_summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--model-root", "--run-dir", "--input-summary", type=Path, required=True)
    plan_parser.add_argument("--dev-json", type=Path, required=True)
    plan_parser.add_argument("--retry-dir", type=Path, required=True)
    plan_parser.add_argument("--exclude-question-ids", type=Path)
    plan_parser.add_argument("--expected", "--expected-total", type=int)
    plan_parser.add_argument("--category", action="append", choices=("deferred", "agent_error", "incorrect"))
    merge_parser = subparsers.add_parser("merge")
    merge_parser.add_argument("--model-root", "--run-dir", "--input-summary", type=Path, required=True)
    merge_parser.add_argument("--retry-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "plan":
        payload = build_plan(
            args.model_root,
            args.dev_json,
            args.retry_dir,
            args.exclude_question_ids,
            expected=args.expected,
            categories=tuple(args.category or ("deferred", "agent_error")),
        )
    else:
        payload = merge_results(args.model_root, args.retry_dir)[1]
    print(json.dumps({key: payload[key] for key in ("selected", "completed") if key in payload}))


if __name__ == "__main__":
    main()
