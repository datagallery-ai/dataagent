#!/usr/bin/env python3
"""Fail closed when an OSI import did not vectorize every emitted value."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


def _column_value_count(payload: dict[str, Any]) -> int:
    models = payload.get("semantic_model") or []
    if len(models) != 1:
        raise ValueError("expected exactly one semantic_model")
    extensions = models[0].get("custom_extensions") or []
    if len(extensions) != 1:
        raise ValueError("expected exactly one custom extension")
    nodes = extensions[0].get("data", {}).get("graph", {}).get("nodes", {})
    values = nodes.get("column_values")
    if not isinstance(values, list):
        raise ValueError("missing graph.nodes.column_values")
    return len(values)


def validate_import(yaml_payload: dict[str, Any], response: dict[str, Any]) -> dict[str, int]:
    if response.get("error"):
        raise ValueError(f"OSI import returned error: {response['error']}")
    if response.get("warnings") not in (None, []):
        raise ValueError(f"OSI import returned warnings: {response['warnings']}")

    summary = response.get("vectorFillSummary")
    if not isinstance(summary, dict):
        raise ValueError("missing vectorFillSummary")
    if summary.get("skipped") is not False:
        raise ValueError(f"vector fill was skipped: {summary.get('skipReason')}")
    if summary.get("warnings") not in (None, []):
        raise ValueError(f"vector fill returned warnings: {summary['warnings']}")
    if summary.get("totalSkipped") != 0:
        raise ValueError(f"vector fill skipped {summary.get('totalSkipped')} tasks")

    emitted = _column_value_count(yaml_payload)
    task_results = summary.get("taskResults")
    if not isinstance(task_results, list):
        raise ValueError("missing vectorFillSummary.taskResults")

    counts: dict[str, int] = {"emitted": emitted}
    for prefix in ("data_column_value_desc_", "data_column_value_val_"):
        matches = [task for task in task_results if str(task.get("task", "")).startswith(prefix)]
        if not matches:
            raise ValueError(f"missing vector task family: {prefix}")
        pending = sum(int(task.get("pending", 0)) for task in matches)
        filled = sum(int(task.get("filled", 0)) for task in matches)
        skipped = sum(int(task.get("skipped", 0)) for task in matches)
        if (pending, filled, skipped) != (emitted, emitted, 0):
            raise ValueError(
                f"incomplete {prefix} vector fill: emitted={emitted} "
                f"pending={pending} filled={filled} skipped={skipped}"
            )
        counts[f"{prefix}filled"] = filled
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yaml", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    args = parser.parse_args()
    yaml_payload = yaml.safe_load(args.yaml.read_text())
    response = json.loads(args.response.read_text())
    print(json.dumps(validate_import(yaml_payload, response), sort_keys=True))


if __name__ == "__main__":
    main()
