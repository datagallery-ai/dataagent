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
"""Unit tests for Job envelope build/finalize (P0.5)."""

from __future__ import annotations

import inspect

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.job_tools.submit_subagent import submit_subagent
from dataagent.core.agents.service import AgentService
from dataagent.core.jobs.envelope import (
    build_base_job_envelope,
    envelope_from_tool_context,
    finalize_job_envelope,
)
from dataagent.core.jobs.models import JobResult
from dataagent.core.managers.action_manager.schemas import ToolSchema


def test_subagent_envelope_contains_only_used_fields() -> None:
    """Baseline subagent envelope omits empty optional workspace fields."""
    envelope = build_base_job_envelope(
        "submit_subagent",
        {
            "agent_id": "worker",
            "task": "inspect data",
            "workspace_dir": "",
            "timeout_sec": 30,
        },
    )
    assert envelope == {
        "kind": "agent",
        "agent_id": "worker",
        "task": "inspect data",
        "timeout_sec": 30,
    }


def test_subagent_envelope_normalizes_workspace_dir_alias() -> None:
    """Galatea ``workspace_dir`` maps to Ferry ``workspace_rel_path``."""
    envelope = build_base_job_envelope(
        "submit_subagent",
        {
            "agent_id": "worker",
            "task": "inspect data",
            "workspace_dir": "subagents/abc",
            "timeout_sec": 30,
        },
    )
    assert envelope is not None
    assert envelope.get("workspace_dir") is None
    assert envelope["workspace_rel_path"] == "subagents/abc"


def test_resource_envelope_omits_empty_execution_fields() -> None:
    """Resource baseline envelope keeps only populated execution fields."""
    envelope = build_base_job_envelope(
        "submit_resource_job",
        {"type": "model_training", "timeout_sec": 120},
    )
    assert envelope == {
        "kind": "resource",
        "type": "model_training",
        "timeout_sec": 120,
    }


def test_plugin_can_enrich_non_core_fields() -> None:
    """Injectors may add non-protected envelope fields."""
    base = {
        "kind": "resource",
        "type": "resource",
        "timeout_sec": 120,
    }
    envelope = finalize_job_envelope(
        "submit_resource_job",
        base,
        {
            "run_id": "run-1",
            "phase": "sampling",
            "receipt_ids": ["semantic-1"],
        },
    )
    assert envelope["type"] == "resource"
    assert envelope["receipt_ids"] == ["semantic-1"]
    assert envelope["run_id"] == "run-1"


def test_finalize_rejects_resource_type_overwrite() -> None:
    """Plugins cannot overwrite core-owned resource ``type``."""
    base = build_base_job_envelope(
        "submit_resource_job",
        {"type": "resource", "timeout_sec": 120},
    )
    assert base is not None
    with pytest.raises(ValueError, match="cannot be overwritten"):
        finalize_job_envelope(
            "submit_resource_job",
            base,
            {**base, "type": "sampling"},
        )


def test_finalize_accepts_plugin_incremental_envelope() -> None:
    """Finalize merges plugin-only extras onto the core baseline."""
    base = build_base_job_envelope(
        "submit_subagent",
        {"agent_id": "worker", "task": "do work", "timeout_sec": 60},
    )
    assert base is not None
    envelope = finalize_job_envelope(
        "submit_subagent",
        base,
        {"run_id": "run-99", "phase": "demo"},
    )
    assert envelope["agent_id"] == "worker"
    assert envelope["task"] == "do work"
    assert envelope["run_id"] == "run-99"


def test_finalize_rejects_protected_field_overwrite() -> None:
    """Plugins cannot overwrite core-owned subagent fields."""
    base = build_base_job_envelope(
        "submit_subagent",
        {"agent_id": "worker", "task": "do work", "timeout_sec": 60},
    )
    assert base is not None
    with pytest.raises(ValueError, match="cannot be overwritten"):
        finalize_job_envelope(
            "submit_subagent",
            base,
            {**base, "task": "malicious"},
        )


def test_submit_subagent_schema_excludes_internal_envelope_fields() -> None:
    """LLM schema must not expose internal envelope/context parameters."""
    schema = ToolSchema.from_function(submit_subagent, "submit_subagent")
    param_names = {param.name for param in schema.parameters}
    assert param_names == {"agent_id", "task", "timeout_sec", "workspace_rel_path"}
    assert "_tool_context" not in param_names
    assert "job_envelope" not in param_names
    assert "_job_envelope" not in param_names
    assert "job_envelope" not in inspect.signature(submit_subagent).parameters


def test_envelope_from_tool_context_reads_execution_context() -> None:
    """Tools can read finalized envelope from ``ToolExecutionContext``."""
    ctx = ToolExecutionContext(job_envelope={"kind": "agent", "agent_id": "a", "task": "t", "timeout_sec": 30})
    assert envelope_from_tool_context(ctx)["agent_id"] == "a"


def test_agent_service_persists_finalized_envelope_metadata(tmp_path) -> None:
    """``AgentService.submit`` stores finalized envelope on ``job.json`` metadata."""

    class _Adapter:
        def run(self, **kwargs) -> JobResult:
            return JobResult(
                job_id=kwargs["job_id"],
                agent_id="arith",
                status="completed",
                summary="ok",
                subagent_session_id=kwargs["subagent_session_id"],
                workspace_rel_path=kwargs["workspace_rel_path"],
            )

    parent_ws = tmp_path / "parent"
    parent_ws.mkdir()
    subagent_yaml = tmp_path / "arith.yaml"
    subagent_yaml.write_text(
        "AGENT_CONFIG:\n  id: arith\n  name: arith\n  description: math\n",
        encoding="utf-8",
    )

    from types import SimpleNamespace

    from dataagent.core.agents.registry import AgentRegistry
    from dataagent.core.jobs.file_store import FileJobStore
    from dataagent.core.jobs.service import JobService

    registry = AgentRegistry.from_subagent_configs([{"path": str(subagent_yaml)}])
    job_service = JobService(FileJobStore(parent_ws))
    runtime = SimpleNamespace(
        workspace_dir=parent_ws,
        session_id="parent_sess",
        env=SimpleNamespace(config_manager=SimpleNamespace(get=lambda *_args, **_kwargs: 4)),
    )
    service = AgentService(registry=registry, job_service=job_service, runtime=runtime, adapter=_Adapter())

    base = build_base_job_envelope("submit_subagent", {"agent_id": "arith", "task": "1+1", "timeout_sec": 30})
    assert base is not None
    enriched = finalize_job_envelope(
        "submit_subagent",
        base,
        {**base, "run_id": "run-42", "phase": "demo"},
    )
    handle = service.submit(agent_id="arith", task="1+1", timeout_sec=30, job_envelope=enriched)
    assert handle["status"] == "queued"
    job_id = handle["job_id"]
    status = job_service.store.read_status(job_id)
    metadata = status.get("metadata") or {}
    stored = metadata.get("job_envelope") or {}
    assert stored.get("run_id") == "run-42"
    assert stored.get("phase") == "demo"
    assert stored.get("agent_id") == "arith"
