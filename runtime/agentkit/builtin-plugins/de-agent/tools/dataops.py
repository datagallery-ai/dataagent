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
"""Native tools for source probing, scratch-table trials and read-only quality checks."""

import asyncio

from langchain.tools import ToolRuntime
from utils.dataops_mcp import dataops_options, dataops_settings
from utils.dataops_probe_tool import dataops_probe_table as _probe
from utils.dataops_trial_compute_tool import trial_compute_once as _trial
from utils.dataops_validate_data_tool import validate_data as _validate_data
from utils.evidence import output_file

from dataagent.models import ModelContext


async def dataops_probe_table(
    table: str,
    runtime: ToolRuntime[ModelContext],
    columns: list[str] | None = None,
    partition_filter: str | None = None,
    sample_size: int = 5,
    timeout_sec: int = 60,
) -> dict:
    """Read explicit columns from one source table (default five rows, maximum 50).

    partition_filter accepts only pt_d='YYYYMMDD'; omit it to use seven days ago.
    Do not pass scheduler placeholders. Failure does not prove that a table is absent.
    """
    with dataops_settings(runtime.context) as context:
        if dataops_options(context).get("enabled", True) is False:
            return {"ok": False, "skipped": True, "reason": "dataops_disabled", "table": table}
        try:
            async with asyncio.timeout(max(1, min(timeout_sec, 300))):
                return await _probe(
                    table,
                    columns,
                    partition_filter=partition_filter,
                    sample_size=sample_size,
                    timeout_sec=timeout_sec,
                    context=context,
                )
        except TimeoutError:
            return {"ok": False, "skipped": True, "reason": "timed_out", "table": table}


async def trial_compute_table(
    runtime: ToolRuntime[ModelContext],
    target_table: str = "",
    dml_path: str | None = None,
    query: str = "",
) -> dict:
    """Execute an INSERT artifact in a scratch table, check its data, then clean up.

    dml_path identifies a session output file. Uses the delivered CREATE artifact
    and a partition from seven days ago. Quality warnings do not block delivery.
    """
    with dataops_settings(runtime.context) as context:
        if dataops_options(context).get("enabled", True) is False:
            return {"passed": False, "skipped": True, "reason": "dataops_disabled"}
        path = str(output_file(runtime.context, dml_path)) if dml_path else None
        if path is None:
            return {"passed": True, "skipped": True, "reason": "no_dml_path"}
        result, _ = await _trial(
            target_table, dml_path=path, query=query, state=runtime.state, context=context
        )
        return result


async def dataops_validate_data(
    runtime: ToolRuntime[ModelContext],
    sql: str = "",
    sql_file_path: str | None = None,
    target_table: str = "",
    partition_filter: str = "",
) -> dict:
    """Read-only data quality checks on the real target table identified by INSERT SQL.

    Does not execute the INSERT or create/drop tables. Prefer sql_file_path for a
    session output artifact. Optional target_table/partition_filter override extraction.
    """
    with dataops_settings(runtime.context) as context:
        if dataops_options(context).get("enabled", True) is False:
            return {"ok": False, "skipped": True, "reason": "dataops_disabled"}
        path = str(output_file(runtime.context, sql_file_path)) if sql_file_path else None
        return await _validate_data(
            sql,
            sql_file_path=path,
            target_table=target_table,
            partition_filter=partition_filter,
            context=context,
        )
