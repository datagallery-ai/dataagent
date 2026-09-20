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
"""Unit tests for :mod:`dataagent.actions.tools.local_tool.dataops_prod_adapter`.

Covers:
- Status mapping (Running/Success/Failed -> running/completed/failed)
- Field extraction (queryJobId, errorLog, data: {headers, rows})
- Error handling (call_tool raises -> success=False)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from dataagent.actions.tools.local_tool import dataops_prod_adapter as pa

# ---------------------------------------------------------------------------
# prod_execute_sql
# ---------------------------------------------------------------------------


class TestProdExecuteSql:
    @pytest.mark.asyncio
    async def test_success_returns_queryjob_id_as_string(self) -> None:
        """Successful execute_sql returns success=True and string job_id."""
        mock_result = {"success": True, "data": {"queryJobId": 12345}}
        with patch.object(pa, "_call_official_mcp", AsyncMock(return_value=mock_result)):
            result = await pa.prod_execute_sql("SELECT 1")

        assert result["success"] is True
        assert result["job_id"] == "12345"
        assert result["raw_data"] == {"queryJobId": 12345}
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_mcp_call_failure(self) -> None:
        """When _call_official_mcp returns success=False, prod_execute_sql propagates error."""
        mock_result = {"success": False, "error": "network down"}
        with patch.object(pa, "_call_official_mcp", AsyncMock(return_value=mock_result)):
            result = await pa.prod_execute_sql("SELECT 1")

        assert result["success"] is False
        assert result["job_id"] is None
        assert result["raw_data"] is None
        assert result["error"] == "network down"

    @pytest.mark.asyncio
    async def test_missing_queryjob_id(self) -> None:
        """Successful MCP call but no queryJobId -> success=False."""
        mock_result = {"success": True, "data": {"sql": "SELECT 1"}}
        with patch.object(pa, "_call_official_mcp", AsyncMock(return_value=mock_result)):
            result = await pa.prod_execute_sql("SELECT 1")

        assert result["success"] is False
        assert result["job_id"] is None
        assert "no queryJobId" in (result["error"] or "")


# ---------------------------------------------------------------------------
# prod_get_query_result
# ---------------------------------------------------------------------------


class TestProdGetQueryResult:
    @pytest.mark.asyncio
    async def test_status_running(self) -> None:
        mock_result = {"success": True, "data": {"status": "Running", "queryJobId": 1}}
        with patch.object(pa, "_call_official_mcp", AsyncMock(return_value=mock_result)):
            result = await pa.prod_get_query_result("1")

        assert result["status"] == "running"
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_status_success(self) -> None:
        mock_result = {"success": True, "data": {"status": "Success"}}
        with patch.object(pa, "_call_official_mcp", AsyncMock(return_value=mock_result)):
            result = await pa.prod_get_query_result("1")

        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_status_failed(self) -> None:
        mock_result = {"success": True, "data": {"status": "Failed"}}
        with patch.object(pa, "_call_official_mcp", AsyncMock(return_value=mock_result)):
            result = await pa.prod_get_query_result("1")

        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_unknown_status_maps_to_error(self) -> None:
        """Unknown status like 'Cancelled' should map to internal 'error'."""
        mock_result = {"success": True, "data": {"status": "Cancelled"}}
        with patch.object(pa, "_call_official_mcp", AsyncMock(return_value=mock_result)):
            result = await pa.prod_get_query_result("1")

        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_mcp_call_failure(self) -> None:
        mock_result = {"success": False, "error": "timeout"}
        with patch.object(pa, "_call_official_mcp", AsyncMock(return_value=mock_result)):
            result = await pa.prod_get_query_result("1")

        assert result["status"] == "error"
        assert result["error"] == "timeout"


# ---------------------------------------------------------------------------
# prod_collect_result
# ---------------------------------------------------------------------------


class TestProdCollectResult:
    @pytest.mark.asyncio
    async def test_completed_converts_headers_rows_to_list_of_dicts(self) -> None:
        polled = {
            "status": "completed",
            "raw": {
                "data": {
                    "headers": ["id", "name"],
                    "rows": [[1, "alice"], [2, "bob"]],
                },
            },
            "error": None,
        }
        with patch.object(pa, "prod_get_query_result", AsyncMock(return_value=polled)):
            result = await pa.prod_collect_result("job-1")

        assert result["status"] == "completed"
        assert result["data"] == [
            {"id": 1, "name": "alice"},
            {"id": 2, "name": "bob"},
        ]
        assert result["data_meta"] is None
        assert result["errorLog"] is None

    @pytest.mark.asyncio
    async def test_completed_with_empty_payload(self) -> None:
        polled = {"status": "completed", "raw": {"data": None}, "error": None}
        with patch.object(pa, "prod_get_query_result", AsyncMock(return_value=polled)):
            result = await pa.prod_collect_result("job-2")

        assert result["status"] == "completed"
        assert result["data"] == []

    @pytest.mark.asyncio
    async def test_failed_inlines_error_log(self) -> None:
        polled = {
            "status": "failed",
            "raw": {"errorLog": "Table 'xxx' does not exist at line 1"},
            "error": None,
        }
        with patch.object(pa, "prod_get_query_result", AsyncMock(return_value=polled)):
            result = await pa.prod_collect_result("job-3")

        assert result["status"] == "failed"
        assert result["errorLog"] == "Table 'xxx' does not exist at line 1"
        assert result["error"] == "Table 'xxx' does not exist at line 1"
        assert result["data"] is None

    @pytest.mark.asyncio
    async def test_failed_with_no_error_log(self) -> None:
        polled = {"status": "failed", "raw": {}, "error": None}
        with patch.object(pa, "prod_get_query_result", AsyncMock(return_value=polled)):
            result = await pa.prod_collect_result("job-4")

        assert result["status"] == "failed"
        assert result["errorLog"] == ""
        assert "status=failed" in (result["error"] or "")

    @pytest.mark.asyncio
    async def test_error_status(self) -> None:
        polled = {"status": "error", "raw": {}, "error": "mcp down"}
        with patch.object(pa, "prod_get_query_result", AsyncMock(return_value=polled)):
            result = await pa.prod_collect_result("job-5")

        assert result["status"] == "error"
        assert result["error"] == "mcp down"
        assert result["errorLog"] is None


# ---------------------------------------------------------------------------
# _call_official_mcp error path
# ---------------------------------------------------------------------------


class TestCallOfficialMcp:
    @pytest.mark.asyncio
    async def test_returns_error_dict_when_call_raises(self) -> None:
        """When call_tool raises, _call_official_mcp returns success=False.

        We patch the client's ``_execute_with_connection`` to raise and
        verify the function still returns a well-formed error dict.
        """
        fake_client = AsyncMock()
        fake_client._execute_with_connection.side_effect = ConnectionError("boom")

        with patch(
            "dataagent.actions.tools.local_tool.dataops_official_mcp_tool.create_mcp_client", return_value=fake_client
        ):
            result = await pa._call_official_mcp("dataops_execute_sql", {"sql": "x"})

        assert result == {"success": False, "error": "boom"}
