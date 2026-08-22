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
"""Unit tests for REST interface boundary security behavior."""

from __future__ import annotations

from typing import Any

import pytest

from dataagent.interface.rest_api.service import DataAgentService


def test_normalize_structured_error_uses_public_field_allowlist() -> None:
    """Structured errors must not expose internal diagnostic fields."""
    service = DataAgentService()
    error = {
        "success": False,
        "code": "NL2SQL-SQL-001",
        "message": "SQL service request failed",
        "http_status": 502,
        "component": "sql_service",
        "retryable": True,
        "detail": "connection failed for postgresql://admin:secret@internal/db",
        "traceback": 'File "/srv/dataagent/sql_service.py", line 101',
        "config": {"password": "secret"},
        "schema": {"private_table": ["customer_ssn"]},
    }

    result = service._normalize_error_payload(error)

    assert result == {
        "result": {
            "success": False,
            "code": "NL2SQL-SQL-001",
            "message": "SQL service request failed",
            "http_status": 502,
            "component": "sql_service",
            "retryable": True,
        }
    }


@pytest.mark.parametrize(
    ("state", "expected_message"),
    [
        (
            {
                "success": False,
                "message": "Agent failed",
                "workspace": "/srv/private",
                "database": {"host": "internal-db", "password": "secret"},
            },
            "Agent failed",
        ),
        (
            {
                "error": RuntimeError("password=secret path=/srv/private/config.yaml"),
            },
            "Agent failed",
        ),
        (
            "password=secret path=/srv/private/config.yaml",
            "Agent returned an invalid result",
        ),
    ],
)
def test_format_result_does_not_expose_internal_state_or_raw_errors(state: Any, expected_message: str) -> None:
    """Failed workflow state and raw errors must not be attached to the API response."""
    service = DataAgentService()

    result = service._format_result(state)

    assert result["result"]["message"] == expected_message
    assert "secret" not in str(result)
    assert "/srv/private" not in str(result)
