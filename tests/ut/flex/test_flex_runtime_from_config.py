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
from dataagent.core.flex.flex_runtime_from_config import resolve_llm_config_entry


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


def test_resolve_llm_config_entry_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAILIAN_BASE_URL", "https://from-env/v1")
    monkeypatch.delenv("BAILIAN_API_KEY", raising=False)

    with pytest.raises(ValueError, match="Missing API key"):
        resolve_llm_config_entry(model_section=_model_section(), entry={"name": "chat_model"})
