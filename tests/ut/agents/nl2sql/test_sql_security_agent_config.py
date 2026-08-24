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
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from dataagent.agents.nl2sql.agent import NL2SQLAgent
from dataagent.agents.nl2sql.nodes.generator import GeneratorNode
from dataagent.agents.nl2sql.nodes.reflector import ReflectorNode
from dataagent.agents.nl2sql.nodes.validator import ValidatorNode
from dataagent.config.config_manager import ConfigManager


def _config_manager(config: dict) -> ConfigManager:
    manager = ConfigManager()
    manager.settings = config
    return manager


def test_default_yaml_omits_sql_security_switch() -> None:
    """Bundled NL2SQL configuration should not expose a SQL-security switch."""
    config_path = Path("dataagent/agents/nl2sql/nl2sql_agent.yaml")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert "sql_security_enabled" not in config.get("CORE", {}).get("validator", {})


def test_from_config_keeps_generator_decoupled_from_sql_security() -> None:
    """Agent construction should not add SQL-security behavior to Generator."""
    config = {
        "CORE": {
            "perceptor": {},
            "generator": {},
            "validator": {},
            "reflector": {},
        },
        "DATABASE": {"dialect": "postgres"},
    }

    agent = NL2SQLAgent.from_config(config, config_manager=_config_manager(config))

    generator = next(node for node in agent.nodes if isinstance(node, GeneratorNode))
    validator = next(node for node in agent.nodes if isinstance(node, ValidatorNode))
    reflector = next(node for node in agent.nodes if isinstance(node, ReflectorNode))
    assert agent.sql_security_enabled is True
    assert not hasattr(generator, "defer_sql_output")
    assert not hasattr(validator, "sql_security_enabled")
    assert not hasattr(reflector, "sql_security_enabled")


def test_from_config_requires_reflector_when_validator_is_configured() -> None:
    """Agent construction should reject a secured Validator workflow without Reflector."""
    config = {
        "CORE": {
            "perceptor": {},
            "generator": {},
            "validator": {},
        },
        "DATABASE": {"dialect": "postgres"},
    }

    with pytest.raises(ValueError, match="reflector"):
        NL2SQLAgent.from_config(config, config_manager=_config_manager(config))


def test_from_config_forces_sql_security_for_validator_workflow() -> None:
    """Validator and Reflector should always use the SQL security path."""
    config = {
        "CORE": {
            "perceptor": {},
            "generator": {},
            "validator": {},
            "reflector": {},
        },
        "DATABASE": {"dialect": "postgres"},
    }

    agent = NL2SQLAgent.from_config(config, config_manager=_config_manager(config))

    validator = next(node for node in agent.nodes if isinstance(node, ValidatorNode))
    reflector = next(node for node in agent.nodes if isinstance(node, ReflectorNode))
    assert agent.sql_security_enabled is True
    assert not hasattr(validator, "sql_security_enabled")
    assert not hasattr(reflector, "sql_security_enabled")


def test_from_config_explicit_false_without_reflector_still_fails() -> None:
    """A stale false setting must not restore a Validator workflow without Reflector."""
    config = {
        "CORE": {
            "perceptor": {},
            "generator": {},
            "validator": {"sql_security_enabled": False},
        },
        "DATABASE": {"dialect": "postgres"},
    }

    with pytest.raises(ValueError, match="reflector"):
        NL2SQLAgent.from_config(config, config_manager=_config_manager(config))


def test_from_config_minimal_core_without_reflector_succeeds() -> None:
    """Minimal CORE (no validator/reflector) must construct for lightweight tests."""
    config = {
        "CORE": {"perceptor": {}, "generator": {}},
        "DATABASE": {"dialect": "postgres"},
    }

    agent = NL2SQLAgent.from_config(config, config_manager=_config_manager(config))

    assert agent.sql_security_enabled is False
    assert not any(isinstance(node, ValidatorNode) for node in agent.nodes)
    assert not any(isinstance(node, ReflectorNode) for node in agent.nodes)


def test_from_config_explicit_false_cannot_disable_security() -> None:
    """A stale sql_security_enabled=false setting should be ignored."""
    config = {
        "CORE": {
            "perceptor": {},
            "generator": {},
            "validator": {"sql_security_enabled": False},
            "reflector": {},
        },
        "DATABASE": {"dialect": "postgres"},
    }

    agent = NL2SQLAgent.from_config(config, config_manager=_config_manager(config))

    validator = next(node for node in agent.nodes if isinstance(node, ValidatorNode))
    reflector = next(node for node in agent.nodes if isinstance(node, ReflectorNode))
    assert agent.sql_security_enabled is True
    assert not hasattr(validator, "sql_security_enabled")
    assert not hasattr(reflector, "sql_security_enabled")


@pytest.mark.parametrize(
    "relpath",
    [
        "dataagent/agents/nl2sql/business_twin.yaml",
        "dataagent/agents/nl2sql/traffic_insight.yaml",
    ],
)
def test_0930_scenario_yaml_omits_security_key_but_has_reflector(relpath: str) -> None:
    """0930 scenario YAML should omit the removed switch and retain Reflector."""
    config = yaml.safe_load(Path(relpath).read_text(encoding="utf-8"))
    core = config.get("CORE", {})
    assert "sql_security_enabled" not in (core.get("validator") or {})
    assert "reflector" in core


def test_unsafe_session_id_dump_stays_under_user_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path))
    monkeypatch.delenv("DATAAGENT_FLEX_PERSISTENCE_ROOT", raising=False)
    dump_dir = NL2SQLAgent._create_context_dump_dir(
        SimpleNamespace(config={}),
        user_id="anonymous",
        session_id="../escaped",
        workspace=None,
        run_id=0,
    )
    assert dump_dir.resolve().is_relative_to((tmp_path / "anonymous").resolve())
    assert "escaped" not in dump_dir.parts
