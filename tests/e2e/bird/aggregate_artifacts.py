"""Summarize captured NL2SQL artifacts without printing SQL or source data."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def read_lines(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def aggregate(root: Path, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    records = []
    strategies = Counter()
    tables = Counter()
    columns = Counter()
    for context in sorted(root.rglob("nl2sql_context_dump")):
        if output == context or output in context.parents:
            continue
        links = read_lines(context / "schema_linking.jsonl")
        generations = read_lines(context / "generator_sql.jsonl")
        final = read_json(context / "final_result.json", {})
        state = read_json(context / "final_state.json", {})
        case_id = final.get("case_id") or next(
            (x.get("case_id") for x in links + generations if x.get("case_id")),
            str(context.parent.relative_to(root)),
        )
        recalled_tables = sorted({t for x in links for t in x.get("tables", [])})
        recalled_columns = sorted({c for x in links for c in x.get("columns", [])})
        candidates = []
        for generation in generations:
            for sql in generation.get("sqls", []):
                candidates.append(
                    {
                        **{
                            k: generation.get(k)
                            for k in (
                                "strategy",
                                "round",
                                "slot",
                                "invocation_id",
                                "generation_index",
                            )
                        },
                        "sql": sql,
                    }
                )
                strategies[generation.get("strategy", "unknown")] += 1
        tables.update(recalled_tables)
        columns.update(recalled_columns)
        records.append(
            {
                "case_id": case_id,
                "artifact_path": str(context.relative_to(root)),
                "schema_linking_events": links,
                "recalled_tables": recalled_tables,
                "recalled_columns": recalled_columns,
                "generator_calls": generations,
                "generated_sql": candidates,
                "final_result": final,
                "capture_errors": state.get("capture_errors", []),
                "has_final_state": bool(state),
            }
        )
    with (output / "case_artifacts.jsonl").open("w") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    with (output / "case_counts.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "case_id",
                "artifact_path",
                "schema_events",
                "tables",
                "columns",
                "generator_calls",
                "sql_candidates",
                "final_state",
                "capture_errors",
            ]
        )
        for record in records:
            writer.writerow(
                [
                    record["case_id"],
                    record["artifact_path"],
                    len(record["schema_linking_events"]),
                    len(record["recalled_tables"]),
                    len(record["recalled_columns"]),
                    len(record["generator_calls"]),
                    len(record["generated_sql"]),
                    record["has_final_state"],
                    len(record["capture_errors"]),
                ]
            )
    ids = Counter(record["case_id"] for record in records)
    evaluations = [read_json(path) for path in sorted(root.rglob("cases/*/stats.json"))]
    completed = [item for item in evaluations if item.get("completed")]
    evaluated_ids = Counter((item["db_id"], item["question_id"]) for item in completed)
    strict = sum(bool(item.get("strict_correct")) for item in completed)
    reference = sum(bool(item.get("extra_columns_correct")) for item in completed)
    summary = {
        "artifact_cases": len(records),
        "unique_case_ids": len(ids),
        "duplicate_case_ids": {key: value for key, value in ids.items() if value > 1},
        "cases_with_final_state": sum(x["has_final_state"] for x in records),
        "cases_with_schema": sum(bool(x["schema_linking_events"]) for x in records),
        "sql_candidates_by_strategy": dict(strategies),
        "table_recall_case_counts": dict(tables),
        "column_recall_case_counts": dict(columns),
        "capture_error_cases": sum(bool(x["capture_errors"]) for x in records),
        "evaluation": {
            "terminal_attempts": len(completed),
            "unique_terminal_cases": len(evaluated_ids),
            "duplicate_terminal_cases": [
                {"db_id": key[0], "question_id": key[1], "attempts": value}
                for key, value in evaluated_ids.items()
                if value > 1
            ],
            "strict_correct": strict,
            "reference_correct": reference,
            "runtime_errors": sum(bool(item.get("error")) for item in completed),
            "llm_error_events": sum(item.get("llm_errors", 0) for item in completed),
            "strict_accuracy_terminal": strict / len(completed) if completed else None,
            "reference_accuracy_terminal": reference / len(completed) if completed else None,
            "accuracy_note": "Runtime failures included; pending cases excluded. Duplicate attempts, if any, are reported and not silently merged.",
        },
        "counting_policy": "All captured candidates, including duplicates and unsuccessful cases; "
        "schema counts are case-level unions, not gold-schema recall accuracy.",
    }
    (output / "artifact_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = aggregate(args.root, args.output or args.root / "artifact-report")
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "artifact_cases",
                    "unique_case_ids",
                    "cases_with_final_state",
                    "capture_error_cases",
                )
            }
        )
    )
