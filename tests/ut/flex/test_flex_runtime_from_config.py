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
"""验证 ``resolve_llm_config_entry`` 的 URL / API Key 解析逻辑。"""

from __future__ import annotations

import pytest

from dataagent.core.flex.flex_runtime_from_config import (
    build_llm_configs_from_flex_config,
    resolve_llm_config_entry,
)


def _model_section(**params: object) -> dict:
    return {
        "chat_model": {
            "provider": "bailian",
            "model_type": "chat",
            "params": {"model": "deepseek-v4-flash", **params},
        }
    }


def test_resolve_llm_config_entry_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAILIAN_BASE_URL", "https://from-env/v1")
    monkeypatch.setenv("BAILIAN_API_KEY", "sk-env")

    flat = resolve_llm_config_entry(
        model_section=_model_section(),
        entry={"name": "chat_model"},
    )

    assert flat["model"] == "deepseek-v4-flash"
    assert flat["api_base"] == "https://from-env/v1"
    assert flat["api_key"] == "sk-env"


def test_resolve_llm_config_entry_params_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAILIAN_BASE_URL", "https://from-env/v1")
    monkeypatch.setenv("BAILIAN_API_KEY", "sk-env")

    flat = resolve_llm_config_entry(
        model_section=_model_section(
            base_url="https://from-yaml/v1",
            api_key="sk-yaml",
        ),
        entry={"name": "chat_model"},
    )

    assert flat["api_base"] == "https://from-yaml/v1"
    assert flat["api_key"] == "sk-yaml"


def test_resolve_llm_config_entry_reads_params_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置 .env 时，可从 params 读取 base_url / api_key（quickstart 场景）。"""
    monkeypatch.delenv("BAILIAN_BASE_URL", raising=False)
    monkeypatch.delenv("BAILIAN_API_KEY", raising=False)

    flat = resolve_llm_config_entry(
        model_section=_model_section(
            base_url="https://from-yaml/v1",
            api_key="sk-yaml",
        ),
        entry={"name": "chat_model"},
    )

    assert flat["api_base"] == "https://from-yaml/v1"
    assert flat["api_key"] == "sk-yaml"


def test_resolve_llm_config_entry_missing_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAILIAN_BASE_URL", raising=False)
    monkeypatch.setenv("BAILIAN_API_KEY", "sk-env")

    with pytest.raises(ValueError, match="Missing URL"):
        resolve_llm_config_entry(model_section=_model_section(), entry={"name": "chat_model"})


def _flex_config_with_hooks(*hook_items: dict) -> dict:
    """Minimal Flex config containing planner node and HOOKS entries."""
    return {
        "MODEL": _model_section(),
        "ACTOR_LOOP": [
            {
                "node": "planner",
                "module": "dataagent.core.flex.nodes.planner.Planner",
                "chat_model": {"name": "chat_model"},
            }
        ],
        "HOOKS": {
            "nodes": {
                "executor": {
                    "post": list(hook_items),
                }
            }
        },
    }


def test_build_llm_configs_registers_suite_prefixed_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suite-prefixed hook ``name`` + ``model`` maps to ``llm_configs[full_name]``."""
    monkeypatch.setenv("BAILIAN_BASE_URL", "https://from-env/v1")
    monkeypatch.setenv("BAILIAN_API_KEY", "sk-env")
    hook_name = "example_suite.hooks.custom_hooks.suite_example_with_model"
    config = _flex_config_with_hooks({"name": hook_name, "model": "chat_model"})
    llm_configs = build_llm_configs_from_flex_config(config)
    assert hook_name in llm_configs
    assert llm_configs[hook_name]["model"] == "deepseek-v4-flash"
    assert llm_configs[hook_name]["api_key"] == "sk-env"


def test_build_llm_configs_registers_builtin_hook_with_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Built-in hook short names with ``model`` continue to register ``llm_configs`` entries."""
    monkeypatch.setenv("BAILIAN_BASE_URL", "https://from-env/v1")
    monkeypatch.setenv("BAILIAN_API_KEY", "sk-env")
    config = _flex_config_with_hooks({"name": "pruner", "model": "chat_model"})
    llm_configs = build_llm_configs_from_flex_config(config)
    assert "pruner" in llm_configs
    assert llm_configs["pruner"]["api_base"] == "https://from-env/v1"


def test_build_llm_configs_hook_model_collision_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook ``name`` must not collide with node or MODEL slot keys in ``llm_configs``."""
    monkeypatch.setenv("BAILIAN_BASE_URL", "https://from-env/v1")
    monkeypatch.setenv("BAILIAN_API_KEY", "sk-env")
    config = _flex_config_with_hooks({"name": "planner", "model": "chat_model"})
    with pytest.raises(ValueError, match="collides"):
        build_llm_configs_from_flex_config(config)


def test_resolve_llm_config_entry_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAILIAN_BASE_URL", "https://from-env/v1")
    monkeypatch.delenv("BAILIAN_API_KEY", raising=False)

    with pytest.raises(ValueError, match="Missing API key"):
        resolve_llm_config_entry(model_section=_model_section(), entry={"name": "chat_model"})
