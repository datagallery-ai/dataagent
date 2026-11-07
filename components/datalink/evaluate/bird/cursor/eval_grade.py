#!/usr/bin/env python3
"""Phase 2: score Cursor run JSON against a separate answers file.

Loads agent run results + golden answers, executes both SQLs on SQLite,
writes scored JSON (adds correct / golden_sql / row counts) and summary .md.

Usage (from repo root):
  uv run python evaluate/bird/cursor/eval_grade.py --results evaluate/bird/cursor/eval_results/run_....json
  uv run python evaluate/bird/cursor/eval_grade.py \\
      --results path/to/run.json \\
      --answers evaluate/bird/dev_answers.json \\
      --sqlite-url sqlite:///C:/path/to/california_schools.sqlite
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from eval_common import (
    EvalConfig,
    EvalResult,
    compare_results,
    execute_sql,
    get_engine,
    load_answers,
    load_config,
    load_json_list,
    metrics_from_dict,
    print_summary,
    repo_root_from,
    resolve_config_path,
    result_to_dict,
    write_json,
    write_summary_md,
)
from sqlalchemy.engine import Engine


def grade_one(
    row: dict[str, Any],
    answers: dict[tuple[Any, str], dict[str, Any]],
    config: EvalConfig,
    engine_cache: dict[str, Engine],
) -> EvalResult:
    qid = row.get("question_id")
    db_id = str(row.get("db_id", ""))
    key = (qid, db_id)
    if key not in answers:
        # Fallback: match by question_id only if unique
        matches = [a for (aq, ad), a in answers.items() if aq == qid]
        if len(matches) == 1:
            ans = matches[0]
        else:
            metrics = metrics_from_dict(row.get("metrics"))
            return EvalResult(
                question_id=qid if qid is not None else -1,
                db_id=db_id,
                mode=str(row.get("mode", "")),
                question=str(row.get("question", "")),
                golden_sql="",
                predicted_sql=row.get("predicted_sql"),
                correct=False,
                error=f"No answer for question_id={qid!r} db_id={db_id!r}",
                golden_row_count=None,
                predicted_row_count=None,
                metrics=metrics,
                answer_text=str(row.get("answer_text", "")),
                difficulty=str(row.get("difficulty", "") or ""),
                run_id=str(row.get("run_id", "")),
                agent_id=str(row.get("agent_id", "")),
            )
    else:
        ans = answers[key]

    golden_sql = ans["SQL"]
    predicted_sql = row.get("predicted_sql")
    # Preserve agent-side error; may append SQL errors below.
    error = row.get("error")
    correct: bool | None = None
    golden_count: int | None = None
    pred_count: int | None = None

    try:
        engine = get_engine(db_id, config, engine_cache)
        golden_rows, golden_err = execute_sql(engine, golden_sql)
        if golden_err:
            raise RuntimeError(f"Golden SQL failed: {golden_err}")
        golden_count = len(golden_rows)

        if not predicted_sql:
            correct = False
            if not error:
                error = "No SQL extracted from agent response"
        else:
            pred_rows, pred_err = execute_sql(engine, predicted_sql)
            if pred_err:
                correct = False
                error = f"Predicted SQL failed: {pred_err}"
            else:
                pred_count = len(pred_rows)
                correct = compare_results(golden_rows, pred_rows)
    except Exception as exc:
        correct = False
        error = f"{type(exc).__name__}: {exc}"

    return EvalResult(
        question_id=qid if qid is not None else -1,
        db_id=db_id,
        mode=str(row.get("mode", "")),
        question=str(row.get("question", "")),
        golden_sql=golden_sql,
        predicted_sql=predicted_sql,
        correct=correct,
        error=error,
        golden_row_count=golden_count,
        predicted_row_count=pred_count,
        metrics=metrics_from_dict(row.get("metrics")),
        answer_text=str(row.get("answer_text", "")),
        difficulty=str(row.get("difficulty") or ans.get("difficulty") or ""),
        run_id=str(row.get("run_id", "")),
        agent_id=str(row.get("agent_id", "")),
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Grade Cursor run JSON against Bird answers")
    p.add_argument(
        "--config",
        help="JSON config file (default: eval.config.json next to this script, if present)",
    )
    p.add_argument(
        "--results",
        required=True,
        help="Path to run JSON from eval_run.py",
    )
    p.add_argument(
        "--answers",
        help="Path to answers JSON (question_id / db_id / SQL). "
        "Default: config.answers or evaluate/bird/dev_answers.json",
    )
    p.add_argument(
        "--output",
        help="Scored JSON path (default: <results_stem>_scored.json next to results)",
    )
    p.add_argument("--sqlite-url", help="Single SQLite URL for all questions")
    p.add_argument("--db-dir", help="Base dir for per-db SQLite files")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    config_path = resolve_config_path(args.config, __file__)
    config = load_config(config_path)

    if args.answers:
        config.answers = args.answers
    if args.sqlite_url:
        config.sqlite_url = args.sqlite_url
    if args.db_dir:
        config.db_dir = args.db_dir

    os.chdir(repo_root_from(__file__))

    results_path = Path(args.results)
    if not results_path.is_file():
        raise SystemExit(f"Results file not found: {results_path}")

    answers_path = Path(config.answers)
    if not answers_path.is_file():
        raise SystemExit(
            f'Answers file not found: {answers_path}\nPass --answers PATH or set "answers" in eval.config.json'
        )

    if args.output:
        out_path = Path(args.output)
    else:
        out_path = results_path.with_name(f"{results_path.stem}_scored.json")

    summary_path = out_path.with_suffix(".md")

    run_rows = load_json_list(results_path)
    answers = load_answers(answers_path)
    engine_cache: dict[str, Engine] = {}

    print(f"Config:   {config_path if config_path is not None else '(defaults)'}")
    print(f"Results:  {results_path} ({len(run_rows)} rows)")
    print(f"Answers:  {answers_path} ({len(answers)} entries)")
    print(f"Output:   {out_path}")
    if config.sqlite_url:
        print(f"SQLite:   {config.sqlite_url}")
    else:
        print(f"DB dir:   {config.db_dir or '(per sqlite_databases map)'}")

    scored: list[EvalResult] = []
    for row in run_rows:
        r = grade_one(row, answers, config, engine_cache)
        scored.append(r)
        mark = "OK" if r.correct else "FAIL"
        print(f"  [{mark}] q={r.question_id} mode={r.mode} correct={r.correct}")
        if r.error:
            print(f"         error: {r.error}")

    write_json(out_path, [result_to_dict(r) for r in scored])
    write_summary_md(scored, summary_path)
    print_summary(scored)
    print(f"\nScored results saved to {out_path}")
    print(f"Summary saved to {summary_path}")


if __name__ == "__main__":
    main()
