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
"""DataOps adHoc SQL Validator MCP server.

Run as a standalone process::

    python -m dataagent.mcp_servers.dataops.dataops_mcp_server

or via the bundled launcher ``start_dataops_mcp.sh``.

Exposes the resource-lifecycle tools expected by DataAgent
(``submit_job``/``poll_job``/``collect_job``/``cancel_job``/``get_job_log``)
and delegates SQL validation to the DataOps OpenAPI endpoints
``/v3/adHoc/create`` and ``/v3/adHoc/query``. The server never connects to
the underlying query engine directly.
"""
