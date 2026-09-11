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
from __future__ import annotations

import json
import math
import re
from typing import Any

from dataagent.agents.bird.errors import LLMOutputParseError

SCORE_RESULTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "issues": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "score", "issues"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}

REFLECTOR_RESULTS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "sql": {"type": "string", "minLength": 1},
        },
        "required": ["id", "sql"],
        "additionalProperties": False,
    },
}

_DIRTY_SCORE_PREFIX = re.compile(
    r'("score"\s*:\s*)'
    r"(?:permalink|\.src/?|宿)\s*"
    r"(0(?:\.\d+)?|1(?:\.0+)?)"
    r"(?=\s*[,}])"
)


def load_score_results_json(content: str) -> Any:
    """Load score JSON, narrowly repairing observed junk before a bounded score.

    This is intentionally not a general JSON repairer.  It only removes a
    non-structural, non-numeric prefix immediately before a JSON number in a
    ``score`` field, then leaves strict shape, ID, type, and range validation to
    ``validate_score_results``.
    """
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        repaired, substitutions = _DIRTY_SCORE_PREFIX.subn(r"\1\2", content)
        if not substitutions:
            raise
        payload = json.loads(repaired)
    if isinstance(payload, dict) and set(payload) == {"results"}:
        return payload["results"]
    return payload


def _parse_error(detail: str) -> LLMOutputParseError:
    return LLMOutputParseError(detail=detail)


def _validate_result_list(payload: Any, expected_ids: list[int], required_keys: set[str]) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise _parse_error("Structured JSON result must be an array")
    if len(payload) != len(expected_ids):
        raise _parse_error(f"Structured JSON result count mismatch: expected {len(expected_ids)}, got {len(payload)}")

    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise _parse_error(f"Structured JSON item {index} must be an object")
        if set(item) != required_keys:
            raise _parse_error(
                f"Structured JSON item {index} fields mismatch: expected {sorted(required_keys)}, got {sorted(item)}"
            )
        item_id = item["id"]
        if type(item_id) is not int:
            raise _parse_error(f"Structured JSON item {index} id must be an integer")

    actual_ids = [item["id"] for item in payload]
    if actual_ids != expected_ids:
        raise _parse_error(f"Structured JSON result ids mismatch: expected {expected_ids}, got {actual_ids}")
    return payload


def validate_score_results(payload: Any, expected_ids: list[int]) -> list[dict[str, Any]]:
    """Validate Validator/Selector output without coercing decision fields."""
    results = _validate_result_list(payload, expected_ids, {"id", "score", "issues"})
    for index, item in enumerate(results):
        score = item["score"]
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise _parse_error(f"Structured JSON item {index} score must be a finite number between 0 and 1")
        issues = item["issues"]
        if not isinstance(issues, list) or not all(isinstance(issue, str) for issue in issues):
            raise _parse_error(f"Structured JSON item {index} issues must be an array of strings")
    return results


def validate_reflector_results(payload: Any, expected_ids: list[int]) -> list[dict[str, Any]]:
    """Validate Reflector output without repairing or coercing SQL values."""
    results = _validate_result_list(payload, expected_ids, {"id", "sql"})
    for index, item in enumerate(results):
        sql = item["sql"]
        if not isinstance(sql, str) or not sql.strip():
            raise _parse_error(f"Structured JSON item {index} sql must be a non-empty string")
    return results
