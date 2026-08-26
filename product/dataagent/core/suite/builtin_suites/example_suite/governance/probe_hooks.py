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
"""Governance hooks for the example Suite."""

from __future__ import annotations

from typing import Any


def guard_plan_deletion(inv: Any) -> None:
    """Protect the destructive builtin ``delete_plan`` tool unless explicitly enabled."""
    suite_config = inv.config.get("EXAMPLE_SUITE", {}) if isinstance(inv.config, dict) else {}
    if not bool(suite_config.get("allow_plan_delete")):
        raise ValueError("example_suite blocks delete_plan unless EXAMPLE_SUITE.allow_plan_delete is true")


def inject_create_plan_context(inv: Any) -> None:
    """
    Smoke-test argument injector for a builtin tool.

    The intended production use case for argument injectors is submit/job-service
    metadata injection, for example adding ``_job_envelope`` with plugin-owned
    routing metadata, private URLs, or credential references. That path requires
    ``LocalToolWrapper`` to consume governance-injected ``_job_envelope`` from
    tool args and merge it into ``ToolExecutionContext.job_envelope``.

    This Suite example uses ``create_plan`` only as a stable smoke target because
    it already declares ``_tool_context`` as an internal argument. The wrapper
    replaces the placeholder below with the real ``ToolExecutionContext``.
    """
    inv.tool_args["_tool_context"] = None
