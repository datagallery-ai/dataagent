# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Unit tests for DATAOPS_ENV routing in :mod:`dataops_validate_tool`.

Verifies that ``dataops_validate_sql`` selects the prod or integration path
based on the ``DATAOPS_ENV`` env variable, and that unknown values fall back
to the default (integration) with a warning.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool import dataops_validate_tool as vt


def _ctx() -> ToolExecutionContext:
    """Build a minimal ToolExecutionContext for routing tests."""
    runtime = MagicMock()
    runtime.user_id = "u1"
    return ToolExecutionContext(runtime=runtime)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _get_dataops_env
# ---------------------------------------------------------------------------


class TestGetDataopsEnv:
    def setup_method(self) -> None:
        # Clear env for each test
        os.environ.pop("DATAOPS_ENV", None)

    def test_default_is_integration(self) -> None:
        assert vt._get_dataops_env() == "integration"

    def test_explicit_prod(self) -> None:
        os.environ["DATAOPS_ENV"] = "prod"
        assert vt._get_dataops_env() == "prod"

    def test_explicit_integration(self) -> None:
        os.environ["DATAOPS_ENV"] = "integration"
        assert vt._get_dataops_env() == "integration"

    def test_case_insensitive(self) -> None:
        os.environ["DATAOPS_ENV"] = "PROD"
        assert vt._get_dataops_env() == "prod"

    def test_whitespace_trimmed(self) -> None:
        os.environ["DATAOPS_ENV"] = "  prod  "
        assert vt._get_dataops_env() == "prod"

    def test_invalid_falls_back_to_integration(self) -> None:
        os.environ["DATAOPS_ENV"] = "staging"
        # Should log a warning and return integration
        assert vt._get_dataops_env() == "integration"


# ---------------------------------------------------------------------------
# dataops_validate_sql routing
# ---------------------------------------------------------------------------


class TestDataopsValidateSqlRouting:
    def setup_method(self) -> None:
        os.environ.pop("DATAOPS_ENV", None)

    @pytest.mark.asyncio
    async def test_prod_env_routes_to_prod_path(self) -> None:
        """When DATAOPS_ENV=prod, _dataops_validate_sql_prod is invoked."""
        from unittest.mock import AsyncMock as _AM

        os.environ["DATAOPS_ENV"] = "prod"
        ctx = _ctx()

        prod_mock = _AM(return_value={"passed": True, "job_id": "p-1"})

        with patch.object(vt, "_dataops_validate_sql_prod", prod_mock):
            result = await vt.dataops_validate_sql("SELECT 1", _tool_context=ctx)

        assert prod_mock.await_count == 1
        assert result["job_id"] == "p-1"

    @pytest.mark.asyncio
    async def test_default_env_routes_to_integration_path(self) -> None:
        """Default (DATAOPS_ENV unset) goes through the original integration path."""
        # Skip check returns immediately so we don't need to mock coordinator
        with patch.object(vt, "_check_skip_conditions", return_value=({"passed": True, "skipped": True}, None)):
            result = await vt.dataops_validate_sql("SELECT 1", _tool_context=_ctx())

        assert result["skipped"] is True


# ---------------------------------------------------------------------------
# _build_failure_response_prod
# ---------------------------------------------------------------------------


class TestBuildFailureResponseProd:
    def test_minimal_response_when_no_error_log(self) -> None:
        result = vt._build_failure_response_prod("failed", "j-1", {"error": "boom"}, "SELECT")
        assert result["passed"] is False
        assert result["job_id"] == "j-1"
        assert result["error"] == "boom"
        assert "errorLog" not in result

    def test_inlines_error_log(self) -> None:
        result = vt._build_failure_response_prod(
            "failed", "j-2", {"error": "boom", "errorLog": "stack trace..."}, "SELECT"
        )
        assert result["errorLog"] == "stack trace..."
        assert result["log_source"] == "inline"

    def test_fallback_error_message(self) -> None:
        result = vt._build_failure_response_prod("failed", "j-3", {}, "SELECT")
        assert "rejected SQL" in result["error"]
