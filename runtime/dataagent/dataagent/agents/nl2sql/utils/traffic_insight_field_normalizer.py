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
"""Normalize Traffic Insight LLM field arrays into need_d / need_m using the field catalog."""

from __future__ import annotations

from dataagent.agents.nl2sql.utils.traffic_insight_field_catalog import (
    TIME_PSEUDO_FIELDS,
    TRAFFIC_INSIGHT_FIELD_KINDS,
)
from dataagent.utils.log import logger


def normalize_traffic_insight_fields(payload: object) -> dict[str, set[str]]:
    """Parse LLM field list into dimension/metric sets via catalog kinds.

    Args:
        payload: JSON array of canonical field names from LLM₁.

    Returns:
        ``{"need_d": set[str], "need_m": set[str]}``.

    Raises:
        TypeError: If payload is not a list of strings.
        ValueError: If no valid fields remain after filtering.
    """
    if not isinstance(payload, list):
        raise TypeError(f"Traffic Insight field extraction must return a list, got {type(payload).__name__}")

    seen: list[str] = []
    for item in payload:
        if not isinstance(item, str):
            raise TypeError(f"Traffic Insight field names must be strings, got {type(item).__name__}")
        name = item.strip()
        if not name or name in seen:
            continue
        seen.append(name)

    need_d: set[str] = set()
    need_m: set[str] = set()
    for name in seen:
        lowered = name.casefold()
        if lowered in TIME_PSEUDO_FIELDS or name in TIME_PSEUDO_FIELDS:
            continue
        kind = TRAFFIC_INSIGHT_FIELD_KINDS.get(name)
        if kind is None:
            logger.warning("Traffic Insight field extraction ignored unknown field: {}", name)
            continue
        if kind == "dimension":
            need_d.add(name)
        else:
            need_m.add(name)

    if not need_d and not need_m:
        raise ValueError("Traffic Insight field extraction returned no valid catalog fields")
    return {"need_d": need_d, "need_m": need_m}


def split_fields_by_catalog(field_names: set[str] | list[str]) -> dict[str, set[str]]:
    """Split arbitrary field names into catalog dimensions/metrics."""
    dimensions: set[str] = set()
    metrics: set[str] = set()
    for raw in field_names:
        name = str(raw or "").strip()
        kind = TRAFFIC_INSIGHT_FIELD_KINDS.get(name)
        if kind == "dimension":
            dimensions.add(name)
        elif kind == "metric":
            metrics.add(name)
    return {"dimensions": dimensions, "metrics": metrics}


def assert_need_fields(need_d: set[str], need_m: set[str]) -> None:
    """Raise ValueError when both dimension and metric sets are empty."""
    if not need_d and not need_m:
        raise ValueError("Traffic Insight table recall requires at least one dimension or metric field")
