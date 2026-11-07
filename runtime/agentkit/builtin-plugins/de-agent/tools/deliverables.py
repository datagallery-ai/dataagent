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

import hashlib
import json
from pathlib import Path

from langchain.tools import ToolRuntime
from utils.checks import _validate_ddl_comprehensive, _validate_dml
from utils.data_task_ir.core import get_ir_context
from utils.data_task_ir.runtime import ir_enabled
from utils.dataops_log_utils import bound_error_excerpt
from utils.dataops_mcp import dataops_settings
from utils.dataops_validate_tool import dataops_validate_sql_with_log_analysis
from utils.evidence import model_evidence, output_file

from dataagent.model_stream import collect_model_stream
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
            response = await collect_model_stream(model,
                [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
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
    dataops = {"status": "NOT_RUN", "results": []}
    if not errors:
        dataops = await _validate_dataops(ddl, dml, runtime.context, query)
        errors.extend(dataops.pop("errors", []))
    return {
        "dataops": dataops,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "quality_status": quality_status,
    }


async def _validate_dataops(ddl, dml, context, query: str) -> dict:
    """Preserve main's per-file-pair attempt budget and successful SQL reuse."""
    config = context.get_plugin_config("de-agent").get("dataops", {})
    report = {"status": "NOT_RUN", "results": [], "errors": []}
    if config.get("enabled", True) is False:
        return {**report, "status": "DISABLED"}
    candidate = {"ddl": ddl.read_text(encoding="utf-8"), "dml": dml.read_text(encoding="utf-8")}
    identity = _digest({"ddl": str(ddl), "dml": str(dml)})
    directory = context.session.outputs / ".dataops"
    directory.mkdir(exist_ok=True)
    state_path = directory / f"validation_{identity}.json"
    lock_path = state_path.with_suffix(".lock")
    try:
        lock = lock_path.open("x")
    except FileExistsError:
        return {**report, "status": "BLOCKED", "errors": ["DataOps validation is already running"]}
    lock.close()
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if state.get("passed_sql_sha256") == _digest(candidate):
            return {**report, "status": "PASS", "reused": True}
        with dataops_settings(context):
            for kind, path in (("ddl", ddl), ("dml", dml)):
                previous = state.get(kind, {})
                attempt = previous.get("attempt", 0) + 1
                if attempt > 5:
                    return {
                        **report,
                        "status": "BLOCKED",
                        "exhausted": True,
                        "errors": [f"DataOps {kind}: five attempts exhausted"],
                    }
                state[kind] = {**previous, "attempt": attempt}
                state_path.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                try:
                    result = await dataops_validate_sql_with_log_analysis(
                        sql_file_path=str(path),
                        attempt=attempt,
                        prior_attempts=previous.get("attempts", []),
                        context=context,
                    )
                except Exception as exc:
                    result = {"passed": False, "error": bound_error_excerpt(str(exc))}
                skipped = bool(result.get("skipped"))
                passed = result.get("passed") is True and not skipped
                detail = bound_error_excerpt(
                    result.get("log_analysis_summary")
                    or result.get("error")
                    or result.get("reason")
                    or ""
                )
                record = {
                    "file": kind,
                    "attempt": attempt,
                    "job_id": result.get("job_id"),
                    "status": "SKIPPED" if skipped else "PASS" if passed else "BLOCKED",
                    "detail": detail,
                }
                report["results"].append(record)
                state[kind] = {
                    "attempt": attempt,
                    "attempts": previous.get("attempts", []) + [record],
                }
                state_path.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                if not passed:
                    report.update(status="BLOCKED", exhausted=attempt >= 5)
                    report["errors"].append(f"DataOps {kind}: {detail or 'validation failed'}")
                    break
            else:
                report["status"] = "PASS"
                state["passed_sql_sha256"] = _digest(candidate)
                state_path.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        return report
    finally:
        lock_path.unlink(missing_ok=True)


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
