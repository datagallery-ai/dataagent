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
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from dataagent.interface.rest_api.service import DataAgentService
from dataagent.interface.rest_api.start_service import (
    CONFIG_ENV_NAME,
    ROUTES_ENV_NAME,
    ScenarioTable,
    UnknownScenarioError,
    build_startup_plan,
    main,
    parse_args,
)


def _write_scene(path: Path, scenario: str) -> None:
    path.write_text(yaml.safe_dump({"DATABASE": {"perceptor_type": scenario}}), encoding="utf-8")


def _scene(tmp_path: Path, scenario: str) -> Path:
    path = tmp_path / f"{scenario}.yaml"
    _write_scene(path, scenario)
    return path


class _Agent:
    def __init__(self, label: str) -> None:
        self.type = "react"
        self.label = label

    async def chat(self, query: str, **_kwargs) -> dict[str, list[dict[str, str]]]:
        return {"messages": [{"content": f"{self.label}:{query}"}]}


def test_bare_config_stays_single_agent(tmp_path: Path) -> None:
    path = tmp_path / "only.yaml"
    path.write_text("{}", encoding="utf-8")

    plan = build_startup_plan([str(path)])

    assert plan.table is None
    assert plan.config_path == path.resolve()


def test_named_routes_bind_scenario_to_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    business = _scene(tmp_path, "business_twin")
    traffic = _scene(tmp_path, "traffic_insight")
    monkeypatch.chdir(tmp_path)

    plan = build_startup_plan([f"business_twin={business.name}", f"traffic_insight={traffic.name}"])

    assert plan.table is not None
    assert plan.table.default == "business_twin"
    assert plan.config_path == business.resolve()
    assert plan.table.routes["business_twin"] == business.resolve()
    restored = ScenarioTable.from_json(plan.table.to_json())
    assert restored.default == "business_twin"
    assert restored.routes["business_twin"] == business.resolve()


def test_one_named_config_defaults_to_that_scenario(tmp_path: Path) -> None:
    path = _scene(tmp_path, "business_twin")

    plan = build_startup_plan([f"business_twin={path}"])

    assert plan.table is not None
    assert plan.table.default == "business_twin"


@pytest.mark.parametrize(
    ("configs", "message"),
    [
        (["a.yaml", "b.yaml"], "multiple yaml files require scenario=path"),
        (["plain.yaml", "business_twin=scene.yaml"], "use either one --config path"),
    ],
)
def test_startup_rejects_ambiguous_config_forms(
    tmp_path: Path,
    configs: list[str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("plain.yaml", "a.yaml", "b.yaml", "scene.yaml"):
        (tmp_path / name).write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        build_startup_plan(configs)


def test_duplicate_scenario_name_fails(tmp_path: Path) -> None:
    first = _scene(tmp_path, "business_twin")
    second = tmp_path / "other.yaml"
    _write_scene(second, "business_twin")

    with pytest.raises(ValueError, match="duplicate scenario name: business_twin"):
        build_startup_plan([f"business_twin={first}", f"business_twin={second}"])


def test_duplicate_yaml_path_fails(tmp_path: Path) -> None:
    path = _scene(tmp_path, "business_twin")
    alias = tmp_path / "alias.yaml"
    alias.symlink_to(path)

    with pytest.raises(ValueError, match="already registered as business_twin"):
        build_startup_plan([f"business_twin={path}", f"traffic_insight={alias}"])


def test_scenario_must_match_perceptor_type(tmp_path: Path) -> None:
    path = tmp_path / "scene.yaml"
    _write_scene(path, "traffic_insight")

    with pytest.raises(ValueError, match="DATABASE.perceptor_type"):
        build_startup_plan([f"business_twin={path}"])


def test_first_named_config_is_the_query_default(tmp_path: Path) -> None:
    business = _scene(tmp_path, "business_twin")
    traffic = _scene(tmp_path, "traffic_insight")

    plan = build_startup_plan([f"business_twin={business}", f"traffic_insight={traffic}"])

    assert plan.table is not None
    assert plan.table.default == "business_twin"
    assert plan.config_path == business.resolve()


def test_missing_config_and_blank_name_fail(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="config not found"):
        build_startup_plan([str(tmp_path / "missing.yaml")])
    with pytest.raises(ValueError, match="expected scenario=path"):
        build_startup_plan(["=scene.yaml"])


def test_query_uses_default_and_operation_selects_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    business = _scene(tmp_path, "business_twin")
    traffic = _scene(tmp_path, "traffic_insight")
    plan = build_startup_plan([f"business_twin={business}", f"traffic_insight={traffic}"])
    assert plan.table is not None

    monkeypatch.setattr(
        "dataagent.interface.rest_api.service.DataAgent.from_config",
        staticmethod(lambda path: _Agent(Path(path).stem)),
    )
    service = DataAgentService(table=plan.table)
    service.initialize()

    from dataagent.interface.rest_api import app as rest_app

    rest_app._data_agent_service = service
    with TestClient(rest_app.app) as client:
        old = client.post("/api/agent/query", json={"query": "old", "stream": False})
        named = client.post(
            "/api/agent/operation",
            json={"type": "query", "content": {"query": "named", "scenario": "traffic_insight"}},
        )
        omitted = client.post("/api/agent/operation", json={"type": "query", "content": {"query": "plain"}})
        unknown = client.post(
            "/api/agent/operation",
            json={"type": "query", "content": {"query": "nope", "scenario": "missing"}},
        )

    assert old.status_code == 200
    assert old.json()["result"]["message"] == "business_twin:old"
    assert named.status_code == 200
    assert named.json()["result"]["message"] == "traffic_insight:named"
    assert omitted.json()["result"]["message"] == "business_twin:plain"
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "unknown scenario: missing"


def test_single_config_query_rejects_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "only.yaml"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "dataagent.interface.rest_api.service.DataAgent.from_config",
        staticmethod(lambda _path: _Agent("only")),
    )
    service = DataAgentService(config_path=path)

    result = asyncio.run(service.query("hello"))
    assert result["result"]["message"] == "only:hello"
    with pytest.raises(UnknownScenarioError, match="unknown scenario: business_twin"):
        asyncio.run(service.query("hello", scenario="business_twin"))


def test_parse_args_and_main_publish_the_route_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    business = _scene(tmp_path, "business_twin")
    traffic = _scene(tmp_path, "traffic_insight")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dataagent",
            "--config",
            f"business_twin={business}",
            "--config",
            f"traffic_insight={traffic}",
        ],
    )
    args = parse_args()
    assert args.config == [f"business_twin={business}", f"traffic_insight={traffic}"]

    monkeypatch.setattr(
        "dataagent.interface.rest_api.start_service.resolve_cert_key_passwords",
        lambda cert: {},
    )
    monkeypatch.setattr("dataagent.interface.rest_api.start_service.build_ssl_kwargs", lambda *args, **kwargs: {})
    monkeypatch.setattr("dataagent.interface.rest_api.start_service.uvicorn.run", lambda *args, **kwargs: None)
    main()

    assert Path(os.environ[CONFIG_ENV_NAME]) == business.resolve()
    table = ScenarioTable.from_json(os.environ[ROUTES_ENV_NAME])
    assert table.default == "business_twin"
    assert set(table.routes) == {"business_twin", "traffic_insight"}


def test_main_exits_when_the_table_is_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["dataagent", "--config", str(tmp_path / "missing.yaml")])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2
