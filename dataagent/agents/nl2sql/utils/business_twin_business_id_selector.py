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

import re
from collections.abc import Collection, Mapping, Sequence
from fnmatch import fnmatchcase
from typing import Any

from dataagent.agents.nl2sql.utils.business_twin_business_id_catalog import (
    _BUSINESS_RULES,
    _BUSINESS_SCHEMAS,
    _DIMENSION_FIELDS,
    _IGNORED_TEMPORAL_FIELDS,
    _METRIC_FIELDS,
    _NETWORK_SUBJECT_TERMS,
)
from dataagent.utils.log import logger

_HIGH_RAIL_RULES = _BUSINESS_RULES["high_rail"]
_HIGH_RAIL_RIDE_METRICS = frozenset(_HIGH_RAIL_RULES["ride_metrics"])
_HIGH_RAIL_RIDE_DIMENSIONS = frozenset(_HIGH_RAIL_RULES["ride_dimensions"])
_HIGH_RAIL_USER_METRICS = frozenset(_HIGH_RAIL_RULES["user_metrics"])
_HIGH_RAIL_GROUPED_DIMENSIONS = frozenset(_HIGH_RAIL_RULES["grouped_user_dimensions"])
_PRB_TRIGGER_METRICS = frozenset(_BUSINESS_RULES["prb"]["trigger_metrics"])
_ASSURANCE_TRIGGER_METRICS = frozenset(_BUSINESS_RULES["assurance_scale"]["trigger_metrics"])
_MOS_TRIGGER_METRICS = frozenset(_BUSINESS_RULES["mos_analysis"]["trigger_metrics"])
_MOS_TRIGGER_DIMENSIONS = frozenset(_BUSINESS_RULES["mos_analysis"]["trigger_dimensions"])
_ORDINARY_RULES = _BUSINESS_RULES["ordinary_experience"]
_ORDINARY_EXTENDED_METRICS = frozenset(_ORDINARY_RULES["extended_metrics"])
_ORDINARY_LOAD_DIMENSIONS = frozenset(_ORDINARY_RULES["load_dimensions"])
_WILDCARD_METRIC_FIELDS = tuple(field for field in _METRIC_FIELDS if "*" in field)


def _canonicalize_column(field: str) -> str:
    if field in _METRIC_FIELDS or field in _DIMENSION_FIELDS:
        return field
    return next((pattern for pattern in _WILDCARD_METRIC_FIELDS if fnmatchcase(field, pattern)), field)


def _normalize_columns(payload: object) -> tuple[frozenset[str], frozenset[str]]:
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise ValueError("业务孪生字段抽取结果必须是字符串数组")

    unique = tuple(dict.fromkeys(_canonicalize_column(item.strip()) for item in payload if item.strip()))
    valid: list[str] = []
    unknown: list[str] = []
    for field in unique:
        if field.casefold() in _IGNORED_TEMPORAL_FIELDS:
            continue
        if field not in _METRIC_FIELDS and field not in _DIMENSION_FIELDS:
            unknown.append(field)
            continue
        valid.append(field)

    if unknown:
        logger.warning(f"Ignoring unknown business-twin extraction columns: {', '.join(unknown)}")

    return (
        frozenset(field for field in valid if field in _METRIC_FIELDS),
        frozenset(field for field in valid if field in _DIMENSION_FIELDS),
    )


def _subject_from_question(question: str) -> str:
    normalized = question.upper()
    if "高铁" in question:
        return "high_rail"

    targeted: list[tuple[int, str]] = []
    mentioned: list[tuple[int, str]] = []
    for subject, term in _NETWORK_SUBJECT_TERMS.items():
        for suffix in ("网元", "实例"):
            index = normalized.find(f"{term}{suffix}")
            if index >= 0:
                targeted.append((index, subject))
        index = normalized.find(term)
        if index >= 0:
            mentioned.append((index, subject))

    if targeted:
        return min(targeted)[1]
    if mentioned:
        return min(mentioned)[1]
    return "general"


def _route_by_dimension(dimensions: frozenset[str], routes: Mapping[str, Sequence[str]]) -> Sequence[str]:
    if "default5qi_group" in dimensions:
        return routes["default5qi_group"]
    if "term_brand" in dimensions:
        return routes["term_brand"]
    return routes["default"]


def _preference_groups(extraction: Mapping[str, Any]) -> list[Sequence[str]]:
    metrics: frozenset[str] = extraction["metrics"]
    dimensions: frozenset[str] = extraction["dimensions"]
    groups: list[Sequence[str]] = []

    if extraction["subject"] == "high_rail":
        if metrics & _HIGH_RAIL_RIDE_METRICS or dimensions & _HIGH_RAIL_RIDE_DIMENSIONS:
            groups.append(_HIGH_RAIL_RULES["ride_routes"])
        if metrics - _HIGH_RAIL_RIDE_METRICS - _HIGH_RAIL_USER_METRICS:
            groups.append(_HIGH_RAIL_RULES["experience_routes"])
        if metrics & _HIGH_RAIL_USER_METRICS or (not metrics and dimensions & _HIGH_RAIL_GROUPED_DIMENSIONS):
            if dimensions & _HIGH_RAIL_GROUPED_DIMENSIONS:
                groups.append(_HIGH_RAIL_RULES["grouped_user_routes"])
            groups.append(_HIGH_RAIL_RULES["default_routes"])
        elif not metrics:
            groups.append(_HIGH_RAIL_RULES["default_routes"])

    prb = _BUSINESS_RULES["prb"]
    if metrics & _PRB_TRIGGER_METRICS:
        groups.append(prb["business_ids"])

    assurance = _BUSINESS_RULES["assurance_scale"]
    if metrics & _ASSURANCE_TRIGGER_METRICS:
        groups.append(_route_by_dimension(dimensions, assurance["routes"]))

    mos = _BUSINESS_RULES["mos_analysis"]
    if metrics & _MOS_TRIGGER_METRICS or dimensions & _MOS_TRIGGER_DIMENSIONS:
        groups.append(_route_by_dimension(dimensions, mos["routes"]))

    if metrics & _ORDINARY_EXTENDED_METRICS:
        if "default5qi_group" in dimensions:
            groups.append(_ORDINARY_RULES["default5qi_routes"])
        elif "term_brand" in dimensions:
            groups.append(_ORDINARY_RULES["term_brand_routes"])
        else:
            groups.append(_ORDINARY_RULES["default_routes"])
        return groups
    if dimensions & _ORDINARY_LOAD_DIMENSIONS:
        groups.append(_ORDINARY_RULES["load_routes"])
    if "default5qi_group" in dimensions:
        groups.append(_ORDINARY_RULES["default5qi_routes"])
    if "term_brand" in dimensions:
        groups.append(_ORDINARY_RULES["term_brand_routes"])
    groups.append(_ORDINARY_RULES["default_routes"])
    return groups


def _business_preference(
    extraction: Mapping[str, Any],
    candidate_ids: Collection[str],
) -> list[str]:
    candidates = set(candidate_ids)
    for group in _preference_groups(extraction):
        represented = [business_id for business_id in group if business_id in candidates]
        if represented:
            return represented
    return []


def _business_id_order(business_id: str) -> int:
    match = re.search(r"\d+$", business_id)
    if not match:
        raise ValueError(f"业务ID没有数字后缀: {business_id}")
    return int(match.group())


def _select_business_id(extraction: Mapping[str, Any]) -> str:
    metrics: frozenset[str] = extraction["metrics"]
    dimensions: frozenset[str] = extraction["dimensions"]
    scores: list[dict[str, Any]] = []
    for business_id, schema in _BUSINESS_SCHEMAS.items():
        matched_metrics = metrics & schema["metrics"]
        missing_metrics = metrics - schema["metrics"]
        matched_dimensions = dimensions & schema["dimensions"]
        missing_dimensions = dimensions - schema["dimensions"]
        scores.append(
            {
                "business_id": business_id,
                "exact": not missing_metrics and not missing_dimensions,
                "matched_metrics": matched_metrics,
                "matched_dimensions": matched_dimensions,
                "missing_dimensions": missing_dimensions,
                "extra_metrics": schema["metrics"] - metrics,
                "extra_dimensions": schema["dimensions"] - dimensions,
            }
        )

    forced_business_id = _BUSINESS_RULES["network_element_routes"].get(extraction["subject"])
    if forced_business_id:
        return forced_business_id

    exact_scores = [score for score in scores if score["exact"]]
    if exact_scores:
        pool = exact_scores
    else:
        max_metric_matches = max(len(score["matched_metrics"]) for score in scores)
        pool = [score for score in scores if len(score["matched_metrics"]) == max_metric_matches]
        max_dimension_matches = max(len(score["matched_dimensions"]) for score in pool)
        pool = [score for score in pool if len(score["matched_dimensions"]) == max_dimension_matches]

    preferred = _business_preference(extraction, {score["business_id"] for score in pool})
    preference_ranks = {business_id: index for index, business_id in enumerate(preferred)}
    unpreferred_rank = len(preferred) + 1 if preferred else 0

    def sort_key(score: Mapping[str, Any]) -> tuple[int, ...]:
        preference_rank = preference_ranks.get(score["business_id"], unpreferred_rank)
        extra_field_count = len(score["extra_metrics"]) + len(score["extra_dimensions"])
        if exact_scores:
            return (
                preference_rank,
                extra_field_count,
                len(score["extra_dimensions"]),
                _business_id_order(score["business_id"]),
            )
        return (
            preference_rank,
            len(score["missing_dimensions"]),
            extra_field_count,
            len(score["extra_dimensions"]),
            _business_id_order(score["business_id"]),
        )

    return min(pool, key=sort_key)["business_id"]


def select_business_twin_business_id(question: str, payload: object) -> str:
    """Select one business-twin business ID from canonical columns extracted by the model."""

    metrics, dimensions = _normalize_columns(payload)
    return _select_business_id(
        {
            "metrics": metrics,
            "dimensions": dimensions,
            "subject": _subject_from_question(question),
        }
    )
