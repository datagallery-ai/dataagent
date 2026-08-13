from pathlib import Path
from typing import Any

import yaml

from dataagent.agents.nl2sql.agent import NL2SQLAgent
from dataagent.agents.nl2sql.nodes import PerceptorNode
from dataagent.agents.nl2sql.nodes.business_twin_perceptor import BusinessTwinPerceptorNode
from dataagent.agents.nl2sql.utils.sql_service import build_sql_service
from dataagent.core.managers.prompt_manager import PromptTemplate
from dataagent.utils.constants import NL2SQL_PROMPT_PREFIX


class _Config:
    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings

    def get(self, key: str, default: Any = None) -> Any:
        value: Any = self.settings
        for segment in key.split("."):
            if not isinstance(value, dict) or segment not in value:
                return default
            value = value[segment]
        return value


def test_cloud_core_engine_builds_http_sql_service() -> None:
    service = build_sql_service(
        "cloud_core",
        {"path": "https://sql.example.test/query", "explain_url": None},
    )

    assert service.__class__.__name__ == "CloudCoreService"


def test_business_twin_db_id_routes_to_specialized_perceptor(monkeypatch) -> None:
    monkeypatch.setattr(
        "dataagent.agents.nl2sql.agent.create_workflow_backend",
        lambda **_: object(),
    )
    config = {
        "CORE": {"perceptor": {}, "generator": {}},
        "DATABASE": {
            "db_id": "business_twin",
            "dialect": "postgres",
            "engine": "cloud_core",
        },
        "SEMANTIC_LAYER": {"business_twin": {"table_selection": {"mode": "business_family", "llm_topk": 4}}},
    }

    agent = NL2SQLAgent.from_config(config, config_manager=_Config(config))

    assert isinstance(agent.nodes[0], BusinessTwinPerceptorNode)


def test_other_cloud_core_db_id_uses_default_perceptor(monkeypatch) -> None:
    monkeypatch.setattr(
        "dataagent.agents.nl2sql.agent.create_workflow_backend",
        lambda **_: object(),
    )
    config = {
        "CORE": {"perceptor": {}, "generator": {}},
        "DATABASE": {
            "db_id": "traffic_insight",
            "dialect": "postgres",
            "engine": "cloud_core",
        },
    }

    agent = NL2SQLAgent.from_config(config, config_manager=_Config(config))

    assert type(agent.nodes[0]) is PerceptorNode


def test_metadata_table_uses_configured_db_id(monkeypatch) -> None:
    config = {
        "DATABASE": {"db_id": "business_twin"},
        "SEMANTIC_LAYER": {"business_twin": {"table_selection": {"mode": "business_family", "llm_topk": 4}}},
    }
    node = BusinessTwinPerceptorNode(config_manager=_Config(config))
    requested_tables: list[str] = []

    def get_columns(table_name: str) -> dict[str, dict[str, str]]:
        requested_tables.append(table_name)
        return {f"{table_name}.metric": {"value_type": "double"}}

    monkeypatch.setattr(node, "_get_table_columns_info", get_columns)

    assert node._column_metadata() == {"business_twin.derived_metrics.metric": {"value_type": "double"}}
    assert requested_tables == ["business_twin.derived_metrics"]


def test_scenario_yaml_and_sql_rules_use_business_twin_name() -> None:
    nl2sql_dir = Path(__file__).resolve().parents[2] / "dataagent" / "agents" / "nl2sql"
    config = yaml.safe_load((nl2sql_dir / "business_twin.yaml").read_text(encoding="utf-8"))

    assert config["DATABASE"]["db_id"] == "business_twin"
    assert config["DATABASE"]["engine"] == "cloud_core"
    assert config["CORE"]["perceptor"]["user_sql_rules"] == "sql_rules_business_twin"
    assert "business_twin" in config["SEMANTIC_LAYER"]
    assert "certificate" not in config
    assert (nl2sql_dir / "prompts" / "user" / "sql_rules_business_twin.md").is_file()


def test_business_twin_perceptor_prompt_actions_resolve() -> None:
    actions = (
        "filter_business_twin_business_id_",
        "filter_business_twin_table_family_",
    )

    for action in actions:
        for role in ("system", "user"):
            prompt = PromptTemplate.from_package_relative(f"{NL2SQL_PROMPT_PREFIX}/perceptor/{action}{role}")
            assert prompt.content
