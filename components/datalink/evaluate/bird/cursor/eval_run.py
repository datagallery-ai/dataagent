#!/usr/bin/env python3
"""Phase 1: run Cursor agents on Bird questions (no golden answers / scoring).

Loads questions only (no SQL field). Writes run JSON with predicted SQL + trajectories.
Grade later with eval_grade.py + a separate answers file.

Usage (from repo root):
  uv run python evaluate/bird/cursor/eval_run.py
  uv run python evaluate/bird/cursor/eval_run.py --config evaluate/bird/cursor/eval.config.json --limit 3
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval_common import (
    EvalConfig,
    RunMetrics,
    RunResult,
    build_prompt,
    extract_sql,
    filter_dataset,
    load_config,
    load_json_list,
    repo_root_from,
    resolve_config_path,
    result_to_dict,
    run_cursor_agent,
    write_json,
)


def run_item(item: dict[str, Any], mode: str, config: EvalConfig) -> RunResult:
    with_datalink = mode == "with_datalink"
    db_id = item["db_id"]
    qid = item.get("question_id", -1)

    metrics = None
    answer_text = ""
    run_id = ""
    agent_id = ""
    predicted_sql: str | None = None
    error: str | None = None

    try:
        sqlite_url = ""
        if not with_datalink:
            # Path hint for baseline only; answers are not loaded here.
            sqlite_url = config.sqlite_url_for(db_id)

        prompt = build_prompt(
            item,
            sqlite_url=sqlite_url,
            inject_sqlite_path=not with_datalink,
            with_datalink=with_datalink,
        )
        answer_text, metrics, run_id, agent_id = run_cursor_agent(
            prompt=prompt,
            config=config,
            with_datalink=with_datalink,
        )

        predicted_sql = extract_sql(answer_text)
        if not predicted_sql:
            error = "No SQL extracted from agent response"

    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        metrics = metrics or RunMetrics()

    assert metrics is not None
    return RunResult(
        question_id=qid,
        db_id=db_id,
        mode=mode,
        question=item["question"],
        predicted_sql=predicted_sql,
        error=error,
        metrics=metrics,
        answer_text=answer_text,
        difficulty=str(item.get("difficulty", "") or ""),
        run_id=run_id,
        agent_id=agent_id,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run Cursor agents on Bird questions (no scoring)")
    p.add_argument(
        "--config",
        help="JSON config file (default: eval.config.json next to this script, if present)",
    )
    p.add_argument("--dataset", help="Path to questions JSON (no SQL field)")
    p.add_argument("--output-dir", help="Directory for run result JSON")
    p.add_argument("--sqlite-url", help="SQLite URL for without_datalink path hint")
    p.add_argument("--db-dir", help="Base dir for per-db SQLite files")
    p.add_argument("--mcp-url", default=None, help="DataLink MCP streamable-http URL")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--offset", type=int, default=None)
    p.add_argument("--mode", choices=["with_datalink", "without_datalink", "both"], default="both")
    p.add_argument("--model", default=None)
    p.add_argument("--cwd", default=None)
    return p.parse_args()


def apply_cli_overrides(config: EvalConfig, args: argparse.Namespace) -> None:
    if args.dataset:
        config.dataset = args.dataset
    if args.output_dir:
        config.output_dir = args.output_dir
    if args.sqlite_url:
        config.sqlite_url = args.sqlite_url
    if args.db_dir:
        config.db_dir = args.db_dir
    if args.mcp_url:
        config.mcp_url = args.mcp_url
    if args.limit is not None:
        config.limit = args.limit
    if args.offset is not None:
        config.offset = args.offset
    if args.model:
        config.model = args.model
    if args.cwd:
        config.cwd = args.cwd
    if args.mode == "with_datalink":
        config.modes = ["with_datalink"]
    elif args.mode == "without_datalink":
        config.modes = ["without_datalink"]


def main() -> None:
    args = parse_args()
    config_path = resolve_config_path(args.config, __file__)
    config = load_config(config_path)
    apply_cli_overrides(config, args)

    os.chdir(repo_root_from(__file__))

    items = load_json_list(config.dataset)
    # Refuse combined files that still carry golden SQL (leak risk).
    if any("SQL" in x for x in items):
        raise SystemExit(
            f"Dataset {config.dataset!r} contains a SQL field. "
            "Use questions-only JSON (e.g. evaluate/bird/dev_questions.json)."
        )

    items = filter_dataset(items, config)
    if not items:
        print("No questions matched filters.")
        sys.exit(0)

    out_dir = Path(config.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"run_{stamp}.json"

    print(f"Config:   {config_path if config_path is not None else '(defaults)'}")
    print(f"Dataset:  {config.dataset} ({len(items)} questions)")
    print(f"Modes:    {config.modes}")
    print(f"Cwd:      {config.cwd}")
    print(f"Sandbox:  {config.sandbox_enabled}")
    print(f"MCP URL:  {config.mcp_url}")
    print(f"Output:   {out_path}")
    print("Note:     no answers loaded — scoring is eval_grade.py")

    all_results: list[RunResult] = []

    def flush() -> None:
        write_json(out_path, [result_to_dict(r) for r in all_results])

    for i, item in enumerate(items):
        for mode in config.modes:
            qid = item.get("question_id", i)
            print(f"\n[{i + 1}/{len(items)}] q={qid} db={item['db_id']} mode={mode}")
            t0 = time.time()
            result = run_item(item, mode, config)
            elapsed = time.time() - t0
            all_results.append(result)

            print(f"  tools={result.metrics.tool_call_count} tokens={result.metrics.total_tokens} ({elapsed:.1f}s)")
            if result.error:
                print(f"  error: {result.error}")
            if result.predicted_sql:
                preview = result.predicted_sql[:120].replace("\n", " ")
                print(f"  sql: {preview}...")

            flush()

    print(f"\nRun results saved to {out_path}")
    print(f"Grade with: uv run python evaluate/bird/cursor/eval_grade.py --results {out_path}")


if __name__ == "__main__":
    main()
