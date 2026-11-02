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
"""Validate SQL artifacts with migrated static checks and the host's native model."""

import json
from pathlib import Path

from langchain.tools import ToolRuntime
from utils.checks import _validate_ddl_comprehensive, _validate_dml
from utils.data_task_ir.core import get_ir_context
from utils.data_task_ir.runtime import ir_enabled
from utils.evidence import model_evidence, output_file

from dataagent.models import ModelContext


async def validate_deliverables(
    ddl_path: str,
    dml_path: str,
    query: str,
    runtime: ToolRuntime[ModelContext],
) -> dict:
    """Check explicit CREATE/INSERT files; quality observations are warnings, not proof of equivalence."""
    ddl = output_file(runtime.context, ddl_path)
    dml = output_file(runtime.context, dml_path)
    if not ddl.is_file() or not dml.is_file():
        return {"valid": False, "errors": ["Both DDL and DML artifacts must exist"], "warnings": []}
    errors = _validate_ddl_comprehensive(ddl)
    _validate_dml(dml, errors)
    settings = runtime.context.get_plugin_config("de-agent")
    ir_context = get_ir_context(runtime.context) if ir_enabled(runtime.context) else ""
    warnings = []
    quality_status = "disabled"
    if settings.get("quality_gate", True):
        evidence = model_evidence(runtime.context)
        payload = {
            "question": query,
            "ddl": ddl.read_text(),
            "dml": dml.read_text(),
            "evidence": evidence,
        }
        instruction = (Path(__file__).resolve().parents[1] / "prompts/quality_check.md").read_text(
            encoding="utf-8"
        )
        if ir_context:
            payload["data_task_ir"] = ir_context
            instruction += "\n" + (
                Path(__file__).resolve().parents[1] / "prompts/ir_quality_check.md"
            ).read_text(encoding="utf-8")
        try:
            model = runtime.context.get_model(settings.get("model", "primary"))
            response = await model.ainvoke(
                [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ]
            )
            text = (
                response.content.split("</think>")[-1]
                .strip()
                .removeprefix("```json")
                .removesuffix("```")
                .strip()
            )
            report = json.loads(text)
            warnings.extend(report.get("warnings", []))
            if settings.get("quality_gate_include_suggestions", False):
                warnings.extend(report.get("suggestions", []))
            quality_status = "completed"
        except Exception as exc:
            quality_status = "failed"
            warnings.append(f"Quality review unavailable: {type(exc).__name__}")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "quality_status": quality_status,
    }
