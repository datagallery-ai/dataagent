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
"""Expose the bounded SQL checker through native ToolRuntime and AgentContext."""

from pathlib import Path

from langchain.tools import ToolRuntime
from utils.sql_checkers import SQLCheckerPipeline
from utils.sql_rule_detectors import COVERAGE

from dataagent import AgentContext


async def check_and_repair_sql(
    ddl_path: str,
    dml_path: str,
    runtime: ToolRuntime[AgentContext],
    repair: bool = True,
) -> dict:
    """Check a CREATE/INSERT file pair within this session's outputs directory.

    Code locates and verifies defects; the model only repairs located issues within
    a bounded budget. repair=False detects without invoking the repair model.
    DataOps validates candidate DDL then DML before changes are published. PASS
    permits delivery; BLOCKED/ERROR preserve the original files. report_path points
    to issues, candidate SQL, model requests/responses and verified diffs.
    """
    context = runtime.context
    settings = context.get_plugin_config("de-agent")
    config = dict(settings.get("sql_checkers", {}))
    # The existing DataOps master switch also governs the checker's execution phase.
    if not settings.get("dataops", {}).get("enabled", True):
        config["dataops_validation_enabled"] = False
    pipeline = SQLCheckerPipeline(
        context.session.outputs,
        lambda: context.get_model(config.get("model", settings.get("model", "primary"))),
        Path(__file__).resolve().parents[1] / "skills/sql-rules/references",
        config,
        context=context,
    )
    result = await pipeline.run(ddl_path, dml_path, repair)
    status = result.get("status", "ERROR")
    report_path = str(pipeline.report_dir / "report.json")
    message = f"程序 SQL checker {status}：{result.get('summary', '')}\n报告：{report_path}"
    return {
        "status": status,
        "passed": status == "PASS",
        "original_msg": message,
        "frontend_msg": message,
        "report_path": report_path,
        "changed_files": result.get("changed_files", []),
        "coverage": COVERAGE,
        "model_calls": result.get("model_calls", 0),
        "issues": result.get("issues", []),
        "contract_issues": result.get("contract_issues", []),
        "dataops": result.get("dataops", {}),
    }
