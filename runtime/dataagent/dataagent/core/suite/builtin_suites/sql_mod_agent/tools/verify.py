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
"""SQL Modification Suite — deliverables verification.

Adapts de_agent's verify logic to support DDL-only or DML-only validation.
When only one of DDL/DML is provided, we skip cross-validation checks
(e.g. "DDL field count = INSERT output column count") and only run
applicable checks for the provided script(s).
"""

from pathlib import Path

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.suite.builtin_suites.de_agent.tools.verify import (
    _is_empty,
    _locate_sql,
    _validate_ddl_comprehensive,
    _validate_deliverables_quality_gate,
    _validate_dml,
)


def validate_deliverables(*, _tool_context: ToolExecutionContext) -> dict[str, str]:
    """
    Check if final deliverables satisfy quality requirements. Supports DDL-only,
    DML-only, or both. Call this function when you think you have finished all steps.
    Errors reported from this function are required to be fixed, while suggestions
    reported can be taken into consideration at will. If this tool is not invoked
    for the first time, it can be invoked again only after all the problems detected
    in the previous invocation are resolved.

    Returns:
        dict[str, str], original and frontend message containing if final deliverables
        have passed basic quality checks and any details.
    """
    result, error, warning = _validate_deliverables(workspace_dir=_tool_context.runtime.workspace_dir)
    out = "最终产物校验通过。" if result else "最终产物校验不通过。"
    if error:
        out += "\n" + error
    if warning:
        out += "\n" + warning

    return {
        "original_msg": out,
        "frontend_msg": out,
    }


def _validate_deliverables(workspace_dir: Path) -> tuple[bool, str, str]:
    """校验最终产物 — 支持 DDL-only / DML-only / 两者皆有。"""
    errors = []
    warnings = []

    DDL, DML = _locate_sql(workspace=workspace_dir)

    # --- DDL 校验（可选） ---
    if DDL is not None and not _is_empty(DDL):
        ddl_errors = _validate_ddl_comprehensive(DDL)
        errors.extend(ddl_errors)
    # DDL 缺失不报错，用户可能只提供了 DML

    # --- DML 校验（可选） ---
    if DML is not None and not _is_empty(DML):
        _validate_dml(DML, errors)
    # DML 缺失不报错，用户可能只提供了 DDL

    # --- LLM 质量门禁（至少有一个 SQL 文件时触发） ---
    has_ddl = DDL is not None and not _is_empty(DDL)
    has_dml = DML is not None and not _is_empty(DML)

    if has_ddl or has_dml:
        _validate_deliverables_quality_gate(
            ddl_file=DDL if has_ddl else None,
            dml_file=DML if has_dml else None,
            warnings=warnings,
        )

    err_msg = "Deliverables checks fail. Errors:\n- " + "\n- ".join(errors) if errors else ""
    warn_msg = "Deliverables checks have suggestions:\n- " + "\n- ".join(warnings) if warnings else ""

    return (len(errors) == 0, err_msg, warn_msg)
