#!/usr/bin/env python3
"""Aggregate a BIRD run or compare any collection of model run directories."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from dataagent.core.suite.builtin_suites.bird_benchmark.retry_bird_failures import (
    _load_full,
    case_key,
    load_results,
    merge_results,
    merge_retry_results,
    summarize_results,
)


def load_model(model_dir: Path) -> dict[tuple[str, int], dict]:
    results, _, _ = _load_full(model_dir)
    return {case_key(item): item for item in results}


def load_effective_model(
    model_dir: Path, initial: dict[tuple[str, int], dict], *, prefer_retry_once: bool
) -> dict[tuple[str, int], dict]:
    if not prefer_retry_once:
        return initial
    retry_dir = model_dir / "retry_once"
    plan_path = retry_dir / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else {}
    legacy_plan = (
        isinstance(plan, dict)
        and plan.get("version") in (2, 3)
        and all(field in plan for field in ("cases", "selected", "expected", "full_shards"))
    )
    if legacy_plan:
        payload, _ = merge_results(model_dir, retry_dir, write=False)
        effective = payload["results"]
    else:
        empty_plan = isinstance(plan, dict) and plan.get("cases") == []
        retry = [] if empty_plan and not _has_results(retry_dir) else load_results(retry_dir)
        effective = merge_retry_results(list(initial.values()), retry)
    results = {case_key(item): item for item in effective}
    if set(results) != set(initial):
        raise ValueError(f"effective result keys differ from initial full run in {model_dir}")
    return results


def model_summary(name: str, results: dict[tuple[str, int], dict]) -> dict:
    rows = list(results.values())
    summary = summarize_results(rows)
    summary.pop("results")
    semantic_digests = {item.get("semantic_model_digest") for item in rows}
    preprocess_models = {item.get("semantic_preprocess_model") for item in rows}
    return {
        "model": name,
        **summary,
        "errors": sum(bool(item.get("error")) for item in rows),
        "summed_case_seconds": sum(float(item.get("elapsed_seconds") or 0) for item in rows),
        "semantic_model_digest": next(iter(semantic_digests)) if len(semantic_digests) == 1 else None,
        "semantic_preprocess_model": next(iter(preprocess_models)) if len(preprocess_models) == 1 else None,
    }


def write_deferred_cases(runs_root: Path, name: str, results: dict[tuple[str, int], dict], *, suffix: str = "") -> None:
    deferred = [item for item in results.values() if item.get("deferred") is True]
    (runs_root / f"{name}_deferred_cases{suffix}.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in deferred),
        encoding="utf-8",
    )
    fields = ("question_id", "db_id", "error", "artifact_dir", "context_dump_dir")
    with (runs_root / f"{name}_deferred_cases{suffix}.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(deferred)


def _has_results(path: Path) -> bool:
    return (
        (path / "summary.json").is_file()
        or (path / "full").is_dir()
        or any(shard.is_dir() for shard in path.glob("shard_*"))
    )


def _check_expected(results: dict[tuple[str, int], dict], expected: int | None, name: str) -> None:
    if expected is not None and len(results) != expected:
        raise ValueError(f"incomplete model run {name}: completed {len(results)}, expected {expected}")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_comparison(runs_root: Path, loaded: dict[str, dict[tuple[str, int], dict]]) -> None:
    if len(loaded) < 2:
        return
    names = list(loaded)
    legacy_pair = set(names) == {"glm5.2", "deepseek"}
    if legacy_pair:
        names = ["glm5.2", "deepseek"]
    prefixes = {name: "glm" if legacy_pair and name == "glm5.2" else name for name in names}
    keys = sorted(set().union(*(set(results) for results in loaded.values())))
    rows = []
    for key in keys:
        row = {"case": f"{key[0]}__q{key[1]}", "db_id": key[0], "question_id": key[1]}
        for field, suffix in (("correct", "correct"), ("error", "error"), ("predicted_sql", "sql")):
            for name in names:
                row[f"{prefixes[name]}_{suffix}"] = loaded[name].get(key, {}).get(field)
        if len(names) == 2:
            left, right = (loaded[name].get(key, {}) for name in names)
            row["same_sql"] = bool(left) and bool(right) and left.get("predicted_sql") == right.get("predicted_sql")
        rows.append(row)
    filename = "glm_vs_deepseek.csv" if legacy_pair else "model_comparison.csv"
    with (runs_root / filename).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["case"])
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    roots = parser.add_mutually_exclusive_group(required=True)
    roots.add_argument("--run-dir", type=Path, help="aggregate one benchmark run")
    roots.add_argument("--runs-root", type=Path, help="aggregate model subdirectories")
    parser.add_argument("--expected", "--expected-total", type=int, help="optional expected selected-case count")
    parser.add_argument("--prefer-retry-once", action="store_true")
    parser.add_argument("--model", action="append", dest="models", help="model subdirectory; repeat to select models")
    parser.add_argument(
        "--model-root", action="append", default=[], metavar="NAME=PATH", help="explicit model directory"
    )
    parser.add_argument(
        "--output", type=Path, help="single-run summary output (default: print without writing artifacts)"
    )
    args = parser.parse_args()
    if args.run_dir is not None:
        if args.models or args.model_root:
            parser.error("--model and --model-root require --runs-root")
        initial = load_model(args.run_dir)
        _check_expected(initial, args.expected, str(args.run_dir))
        loaded = load_effective_model(args.run_dir, initial, prefer_retry_once=args.prefer_retry_once)
        summary = summarize_results(list(loaded.values()), selected=len(initial))
        if args.output is not None:
            _write_json(args.output, summary)
        print(json.dumps(summary, ensure_ascii=False))
        return
    if args.output is not None:
        parser.error("--output is only supported with --run-dir")

    model_dirs: dict[str, Path] = {}
    for name in args.models or ():
        if not name or name in {".", ".."} or Path(name).name != name or name in model_dirs:
            parser.error(f"invalid or duplicate model name: {name!r}")
        model_dirs[name] = args.runs_root / name
    for binding in args.model_root:
        name, separator, value = binding.partition("=")
        if (
            not separator
            or not value
            or not name
            or Path(name).name != name
            or name in {".", ".."}
            or name in model_dirs
        ):
            parser.error(f"expected unique NAME=PATH for --model-root: {binding!r}")
        model_dirs[name] = Path(value)
    if not model_dirs:
        if not args.runs_root.is_dir():
            parser.error(f"runs root is not a directory: {args.runs_root}")
        model_dirs = {
            path.name: path for path in sorted(args.runs_root.iterdir()) if path.is_dir() and _has_results(path)
        }
    if not model_dirs:
        parser.error("no model runs found; use --model-root NAME=PATH for explicit locations")
    initial_loaded = {name: load_model(path) for name, path in model_dirs.items()}
    for name, results in initial_loaded.items():
        _check_expected(results, args.expected, name)
    loaded = {
        name: load_effective_model(model_dirs[name], results, prefer_retry_once=args.prefer_retry_once)
        for name, results in initial_loaded.items()
    }
    counts = {len(results) for results in initial_loaded.values()}
    expected = args.expected if args.expected is not None else next(iter(counts)) if len(counts) == 1 else None
    initial_summary = {
        "expected_cases": expected,
        "models": [model_summary(name, results) for name, results in initial_loaded.items()],
    }
    summary = {
        "expected_cases": expected,
        "models": [model_summary(name, results) for name, results in loaded.items()],
    }
    args.runs_root.mkdir(parents=True, exist_ok=True)
    _write_json(args.runs_root / "model_summary_initial.json", initial_summary)
    _write_json(args.runs_root / "model_summary.json", summary)
    for name, results in initial_loaded.items():
        write_deferred_cases(args.runs_root, name, results, suffix="_initial")
    for name, results in loaded.items():
        write_deferred_cases(args.runs_root, name, results)
    _write_comparison(args.runs_root, loaded)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
