from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

SUITE_DIR = Path(__file__).resolve().parents[2]
SCENARIOS_ROOT = SUITE_DIR / "resources"


def load_scenario_steps(scenario_id: str, step_targets_json: str = "") -> list[dict[str, str]]:
    """Load scenario steps and apply optional per-step target overrides from JSON."""
    scenario = _load_scenario(scenario_id)
    overrides = _parse_step_target_overrides(step_targets_json)
    steps = scenario.get("steps")
    if not isinstance(steps, list):
        raise ValueError(f"data analysis scenario `{scenario_id}` has no steps")
    expanded: list[dict[str, str]] = []
    for index, item in enumerate(steps):
        if not isinstance(item, dict):
            raise ValueError(f"scenario step at index {index} must be a dict")
        step_id = str(item.get("id") or "").strip()
        owner_id = str(item.get("owner_id") or "").strip()
        template = str(item.get("target_template") or item.get("target") or "").strip()
        target = str(overrides.get(step_id) or template).strip()
        if not step_id:
            raise ValueError(f"scenario step at index {index} requires id")
        if not owner_id:
            raise ValueError(f"owner_id for scenario step `{step_id}` is required")
        if not target:
            raise ValueError(f"target for scenario step `{step_id}` is required")
        expanded.append({"id": step_id, "owner_id": owner_id, "target": target})
    return expanded


def _load_scenario(scenario_id: str) -> dict[str, Any]:
    clean_id = str(scenario_id or "target_audience_selection").strip() or "target_audience_selection"
    if any(part in {"", ".", ".."} or part.startswith(".") for part in Path(clean_id).parts):
        raise ValueError(f"invalid data analysis scenario: {clean_id}")
    path = _scenario_path(clean_id)
    if not path.is_file():
        raise ValueError(f"unknown data analysis scenario: {clean_id}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"data analysis scenario `{clean_id}` is invalid")
    return payload


def _scenario_path(clean_id: str) -> Path:
    if "/" in clean_id:
        candidate = (SCENARIOS_ROOT / clean_id).with_suffix(".yaml").resolve()
        try:
            candidate.relative_to(SCENARIOS_ROOT.resolve())
        except ValueError as exc:
            raise ValueError(f"invalid data analysis scenario: {clean_id}") from exc
        return candidate
    return SCENARIOS_ROOT / f"{clean_id}.yaml"


def _parse_step_target_overrides(raw: str) -> dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("step_targets_json must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("step_targets_json must be a JSON object")
    return {str(key).strip(): str(value).strip() for key, value in payload.items() if str(key).strip()}
