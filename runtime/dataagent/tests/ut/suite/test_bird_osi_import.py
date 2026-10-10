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
"""Offline checks for synchronous BIRD OSI import statistics."""

from __future__ import annotations

from typing import Any

import pytest

from dataagent.core.suite.builtin_suites.bird_benchmark.validate_osi_import import validate_import


def _payload(emitted: int = 2) -> dict[str, Any]:
    return {
        "semantic_model": [{"custom_extensions": [{"data": {"graph": {"nodes": {"column_values": [{}] * emitted}}}}]}]
    }


def _response(counter: str = "processed", emitted: int = 2) -> dict[str, Any]:
    return {
        "status": "SYNCED",
        "warnings": [],
        "vectorFillSummary": {
            "skipped": False,
            "totalSkipped": 0,
            "warnings": [],
            "taskResults": [
                {"task": f"data_column_value_{family}_bge_base", counter: emitted, "filled": emitted, "skipped": 0}
                for family in ("desc", "val")
            ],
        },
    }


@pytest.mark.parametrize("counter", ["pending", "processed"])
@pytest.mark.parametrize("emitted", [0, 2])
def test_accepts_complete_old_and_new_synchronous_counts(counter: str, emitted: int) -> None:
    assert validate_import(_payload(emitted), _response(counter, emitted)) == {
        "emitted": emitted,
        "data_column_value_desc_filled": emitted,
        "data_column_value_val_filled": emitted,
    }


def test_accepts_matching_pending_and_processed_counts() -> None:
    response = _response()
    for task in response["vectorFillSummary"]["taskResults"]:
        task["pending"] = task["processed"]
    assert validate_import(_payload(), response)["emitted"] == 2


@pytest.mark.parametrize("counter", ["pending", "processed"])
@pytest.mark.parametrize("field", ["attempted", "filled", "skipped"])
def test_rejects_missing_counts_even_when_no_values_emitted(counter: str, field: str) -> None:
    response = _response(counter, emitted=0)
    task = response["vectorFillSummary"]["taskResults"][0]
    del task[counter if field == "attempted" else field]
    with pytest.raises(ValueError, match="invalid .* counter"):
        validate_import(_payload(0), response)


@pytest.mark.parametrize("counter", ["pending", "processed"])
@pytest.mark.parametrize("field", ["attempted", "filled", "skipped"])
@pytest.mark.parametrize("invalid", [None, -1, "0", 0.0, False, True])
def test_rejects_non_integer_or_negative_counts(counter: str, field: str, invalid: Any) -> None:
    response = _response(counter, emitted=0)
    response["vectorFillSummary"]["taskResults"][0][counter if field == "attempted" else field] = invalid
    with pytest.raises(ValueError, match="invalid .* counter"):
        validate_import(_payload(0), response)


@pytest.mark.parametrize("pending", [1, None, "2", True])
def test_rejects_conflicting_or_invalid_pending_when_processed_exists(pending: Any) -> None:
    response = _response()
    response["vectorFillSummary"]["taskResults"][0]["pending"] = pending
    with pytest.raises(ValueError, match="inconsistent pending/processed|invalid pending counter"):
        validate_import(_payload(), response)


@pytest.mark.parametrize("counter", ["pending", "processed"])
@pytest.mark.parametrize("family_index", [0, 1])
@pytest.mark.parametrize("counts", [(1, 1, 0), (2, 1, 0), (2, 1, 1), (0, 0, 0)])
def test_rejects_incomplete_or_repeated_import_counts(counter: str, family_index: int, counts: tuple[int, ...]) -> None:
    response = _response(counter)
    task = response["vectorFillSummary"]["taskResults"][family_index]
    task[counter], task["filled"], task["skipped"] = counts
    with pytest.raises(ValueError, match="incomplete data_column_value_"):
        validate_import(_payload(), response)


@pytest.mark.parametrize("counter", ["pending", "processed"])
def test_rejects_double_model_family_totals(counter: str) -> None:
    response = _response(counter)
    tasks = response["vectorFillSummary"]["taskResults"]
    tasks.extend([{**task, "task": task["task"].replace("bge_base", "bge_m3")} for task in tasks])
    # The validator checks family totals; this is not per-model coverage verification.
    with pytest.raises(ValueError, match="incomplete data_column_value_desc_"):
        validate_import(_payload(), response)


@pytest.mark.parametrize("location", ["response", "summary"])
def test_rejects_warnings(location: str) -> None:
    response = _response()
    target = response if location == "response" else response["vectorFillSummary"]
    target["warnings"] = ["vector fill warning"]
    with pytest.raises(ValueError, match="warnings"):
        validate_import(_payload(), response)


@pytest.mark.parametrize("summary", [None, {}])
def test_rejects_accepted_async_response_without_completed_summary(summary: Any) -> None:
    with pytest.raises(ValueError):
        validate_import(_payload(), {"status": "ACCEPTED", "vectorFillSummary": summary})


@pytest.mark.parametrize("field,value", [("skipped", True), ("totalSkipped", 1)])
def test_rejects_skipped_summary(field: str, value: Any) -> None:
    response = _response()
    response["vectorFillSummary"][field] = value
    with pytest.raises(ValueError, match="skipped"):
        validate_import(_payload(), response)


@pytest.mark.parametrize("tasks", [None, {}, [None], ["invalid"]])
def test_rejects_malformed_task_results(tasks: Any) -> None:
    response = _response()
    response["vectorFillSummary"]["taskResults"] = tasks
    with pytest.raises(ValueError, match="taskResults"):
        validate_import(_payload(), response)


@pytest.mark.parametrize("family_index", [0, 1])
def test_rejects_missing_task_family(family_index: int) -> None:
    response = _response()
    del response["vectorFillSummary"]["taskResults"][family_index]
    with pytest.raises(ValueError, match="missing vector task family"):
        validate_import(_payload(), response)
