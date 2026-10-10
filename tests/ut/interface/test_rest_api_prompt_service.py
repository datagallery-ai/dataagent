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
"""Service-layer tests for the scenario packaged-prompt view/update operations."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from dataagent.agents.nl2sql.utils import prompt_files as prompt_files_module
from dataagent.interface.rest_api import service as service_module
from dataagent.interface.rest_api.service import DataAgentService
from dataagent.interface.rest_api.start_service import ScenarioTable, UnknownScenarioError

#: Content that survives a byte-for-byte round trip: CJK text, quotes, backslashes, a code block.
MARKDOWN = '# 规则\n- 引号 "quoted" 与反斜杠 \\ \n```sql\nSELECT 1;\n```\n'


class _Nl2SQLAgent:
    """Minimal nl2sql-typed agent so failure envelopes use WORKFLOW-AGENT-001."""

    type = "nl2sql"


@pytest.fixture
def package_prompts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect packaged prompt resolution into a temp package tree."""
    root = tmp_path / "package"
    prompts_dir = root / "agents/nl2sql/prompts/user"
    prompts_dir.mkdir(parents=True)
    monkeypatch.setattr(prompt_files_module, "dataagent_package_path", lambda *parts: root.joinpath(*parts))
    return prompts_dir


def _scene_config(file_name: str | None) -> dict[str, Any]:
    """Scenario yaml content; None drops CORE.perceptor.user_sql_rules entirely."""
    perceptor = {} if file_name is None else {"user_sql_rules": file_name}
    return {"CORE": {"perceptor": perceptor}}


def _build_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configs: dict[str, dict[str, Any]]
) -> DataAgentService:
    """Build an initialized multi-scenario service from name → yaml content."""
    monkeypatch.setattr(service_module.DataAgent, "from_config", staticmethod(lambda _path: _Nl2SQLAgent()))
    routes: dict[str, Path] = {}
    for name, config in configs.items():
        yaml_path = tmp_path / f"{name}.yaml"
        yaml_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        routes[name] = yaml_path
    service = DataAgentService(table=ScenarioTable(routes=routes, default=next(iter(routes))))
    service.initialize()
    return service


def _mtime_iso(path: Path) -> str:
    """mtime → UTC ISO 8601, the same formatting prompt_files uses."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def _assert_agent_failure(result: dict[str, Any], message_part: str) -> None:
    """Assert the file-layer failure envelope: WORKFLOW-AGENT-001, http 500, message."""
    payload = result["result"]
    assert payload["success"] is False
    assert payload["code"] == "WORKFLOW-AGENT-001"
    assert payload["http_status"] == 500
    assert payload["component"] == "agent"
    assert payload["retryable"] is False
    assert message_part in payload["message"]


@pytest.mark.asyncio
async def test_view_prompt_returns_markdown_and_mtime(
    tmp_path: Path, package_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """view returns the packaged file verbatim plus its mtime."""
    target = package_prompts_dir / "sql_rules_business_twin.md"
    target.write_text(MARKDOWN, encoding="utf-8")
    service = _build_service(tmp_path, monkeypatch, {"business_twin": _scene_config("sql_rules_business_twin")})

    result = await service.view_prompt("business_twin")

    assert result == {"result": {"success": True, "markdown": MARKDOWN, "updated_at": _mtime_iso(target)}}


@pytest.mark.asyncio
async def test_update_prompt_replaces_whole_file(
    tmp_path: Path, package_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """update replaces the whole file and returns the mtime of the new content."""
    target = package_prompts_dir / "sql_rules_business_twin.md"
    target.write_text("# old rules\n", encoding="utf-8")
    os.utime(target, (1000000000, 1000000000))
    service = _build_service(tmp_path, monkeypatch, {"business_twin": _scene_config("sql_rules_business_twin")})

    result = await service.update_prompt("business_twin", MARKDOWN)

    assert target.read_text(encoding="utf-8") == MARKDOWN
    assert result == {"result": {"success": True, "updated_at": _mtime_iso(target)}}
    assert result["result"]["updated_at"] != datetime.fromtimestamp(1000000000, tz=UTC).isoformat()


@pytest.mark.asyncio
async def test_update_then_view_round_trip(
    tmp_path: Path, package_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A following view reads back exactly the submitted markdown and write time."""
    target = package_prompts_dir / "sql_rules_business_twin.md"
    target.write_text("# old rules\n", encoding="utf-8")
    service = _build_service(tmp_path, monkeypatch, {"business_twin": _scene_config("sql_rules_business_twin")})

    updated = await service.update_prompt("business_twin", MARKDOWN)
    viewed = await service.view_prompt("business_twin")

    assert viewed["result"]["success"] is True
    assert viewed["result"]["markdown"] == MARKDOWN
    assert viewed["result"]["updated_at"] == updated["result"]["updated_at"]
    assert target.read_text(encoding="utf-8") == MARKDOWN


@pytest.mark.asyncio
async def test_update_is_isolated_per_scenario(
    tmp_path: Path, package_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Updating one scenario leaves the other scenario's file and mtime untouched."""
    twin = package_prompts_dir / "sql_rules_business_twin.md"
    traffic = package_prompts_dir / "sql_rules_traffic_insight.md"
    twin.write_text("# twin\n", encoding="utf-8")
    traffic.write_text("# traffic\n", encoding="utf-8")
    traffic_before = _mtime_iso(traffic)
    service = _build_service(
        tmp_path,
        monkeypatch,
        {
            "business_twin": _scene_config("sql_rules_business_twin"),
            "traffic_insight": _scene_config("sql_rules_traffic_insight"),
        },
    )

    await service.update_prompt("business_twin", MARKDOWN)
    viewed = await service.view_prompt("traffic_insight")

    assert viewed == {"result": {"success": True, "markdown": "# traffic\n", "updated_at": traffic_before}}


@pytest.mark.asyncio
async def test_unknown_scenario_raises_for_view_and_update(
    tmp_path: Path, package_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scenario not registered at startup raises instead of returning a failure envelope."""
    package_prompts_dir.joinpath("sql_rules_business_twin.md").write_text("# rules\n", encoding="utf-8")
    service = _build_service(tmp_path, monkeypatch, {"business_twin": _scene_config("sql_rules_business_twin")})

    with pytest.raises(UnknownScenarioError, match="unknown scenario: missing"):
        await service.view_prompt("missing")
    with pytest.raises(UnknownScenarioError, match="unknown scenario: missing"):
        await service.update_prompt("missing", MARKDOWN)


@pytest.mark.asyncio
async def test_empty_scenario_does_not_fall_back_to_default(
    tmp_path: Path, package_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty scenario is rejected instead of selecting the default scenario."""
    package_prompts_dir.joinpath("sql_rules_business_twin.md").write_text("# rules\n", encoding="utf-8")
    service = _build_service(tmp_path, monkeypatch, {"business_twin": _scene_config("sql_rules_business_twin")})

    with pytest.raises(UnknownScenarioError, match="unknown scenario"):
        await service.view_prompt("")
    with pytest.raises(UnknownScenarioError, match="unknown scenario"):
        await service.update_prompt("", MARKDOWN)


@pytest.mark.asyncio
async def test_single_config_mode_rejects_prompt_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-config startup has no scenario to address; view/update stay 422-level errors."""
    monkeypatch.setattr(service_module.DataAgent, "from_config", staticmethod(lambda _path: _Nl2SQLAgent()))
    config = tmp_path / "only.yaml"
    config.write_text("{}", encoding="utf-8")
    service = DataAgentService(config_path=config)

    with pytest.raises(UnknownScenarioError, match="unknown scenario: business_twin"):
        await service.view_prompt("business_twin")
    with pytest.raises(UnknownScenarioError, match="unknown scenario: business_twin"):
        await service.update_prompt("business_twin", MARKDOWN)


@pytest.mark.asyncio
async def test_view_prompt_missing_config_key_returns_agent_failure(
    tmp_path: Path, package_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scenario yaml without user_sql_rules returns the agent failure envelope."""
    service = _build_service(tmp_path, monkeypatch, {"business_twin": _scene_config(None)})

    result = await service.view_prompt("business_twin")

    _assert_agent_failure(result, "CORE.perceptor.user_sql_rules is not configured")


@pytest.mark.asyncio
async def test_update_prompt_rejects_directory_name_and_keeps_file(
    tmp_path: Path, package_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory-bearing config value fails without touching the existing file."""
    target = package_prompts_dir / "sql_rules_business_twin.md"
    target.write_text("# old rules\n", encoding="utf-8")
    service = _build_service(tmp_path, monkeypatch, {"business_twin": _scene_config("../escape")})

    result = await service.update_prompt("business_twin", MARKDOWN)

    _assert_agent_failure(result, "must be a bare file name")
    assert target.read_text(encoding="utf-8") == "# old rules\n"


@pytest.mark.asyncio
async def test_update_prompt_missing_target_file_returns_agent_failure(
    tmp_path: Path, package_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing to a file missing from the package fails and creates nothing."""
    service = _build_service(tmp_path, monkeypatch, {"business_twin": _scene_config("sql_rules_missing")})

    result = await service.update_prompt("business_twin", MARKDOWN)

    _assert_agent_failure(result, "prompt file not found")
    assert not (package_prompts_dir / "sql_rules_missing.md").exists()


@pytest.mark.asyncio
async def test_view_prompt_missing_target_file_returns_agent_failure(
    tmp_path: Path, package_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Viewing a file missing from the package fails the same way as update."""
    service = _build_service(tmp_path, monkeypatch, {"business_twin": _scene_config("sql_rules_missing")})

    result = await service.view_prompt("business_twin")

    _assert_agent_failure(result, "sql_rules_missing")


@pytest.mark.asyncio
async def test_prompt_target_resolved_once_until_restart(
    tmp_path: Path, package_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The target path is cached per (scenario, document); yaml edits need a restart."""
    target = package_prompts_dir / "sql_rules_business_twin.md"
    target.write_text("# v1\n", encoding="utf-8")
    service = _build_service(tmp_path, monkeypatch, {"business_twin": _scene_config("sql_rules_business_twin")})
    calls: list[str] = []
    real_resolver = service_module.editable_prompt_path

    def counting_resolver(document: str, yaml_path: Path) -> Path:
        calls.append(yaml_path.name)
        return real_resolver(document, yaml_path)

    monkeypatch.setattr(service_module, "editable_prompt_path", counting_resolver)

    first = await service.view_prompt("business_twin")
    (tmp_path / "business_twin.yaml").write_text(
        yaml.safe_dump(_scene_config("sql_rules_traffic_insight")), encoding="utf-8"
    )
    second = await service.view_prompt("business_twin")

    assert calls == ["business_twin.yaml"]
    assert second == first
