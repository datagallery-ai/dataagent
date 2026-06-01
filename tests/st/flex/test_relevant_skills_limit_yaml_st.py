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
"""ST：用户 YAML 中的 ``AGENT_CONFIG.relevant_skills_limit`` 经 ``DataAgent.from_config`` 加载后，planner 能用到。

**回归看护（针对 e340860c 类改动）**：若 ``_build_flex_skill_prompt_variables`` 仅从 ``runtime.env``
拼最小 ``AGENT_CONFIG``（只有 ``type``）且不再合并 YAML / ``config_manager``，则
``_get_relevant_skills_limit`` 会得到 ``None``，skill-selector **不会**以 YAML 中的 limit 调用 LLM。
本文件要求：在 **真实** ``Runtime``（``build_runtime_from_flex_config``）上调用
``_build_flex_skill_prompt_variables`` 时，skill-selector 系统提示里仍出现 ``SYS limit=<YAML 值>``。

不 mock ``_select_relevant_skills_for_prompt``；仅对 ``build_llm_configs_from_flex_config`` / ``get_default_llm``
打桩以避免真实网络与 provider 环境依赖。
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import dataagent.core.flex.flex_runtime_from_config as flex_rt
import dataagent.core.flex.utils.planner_prompt_builder as planner_prompt_builder
from dataagent.core.managers.llm_manager.adapters import ChatModel, LLMResponse
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate
from dataagent.interface.sdk.agent import DataAgent


class _FakeChatModel:
    """skill-selector 一次 ``invoke``，返回合法 JSON。"""

    def __init__(self, response_content: str) -> None:
        self._response_content = response_content
        self.calls = 0
        self.last_messages = None

    def invoke(self, messages, **_kwargs):
        self.calls += 1
        self.last_messages = messages
        return LLMResponse(content=self._response_content, usage_metadata={})


def _default_llm(llm: _FakeChatModel) -> ChatModel:
    return cast(ChatModel, llm)


def _inject_skill_selector_prompts(monkeypatch: pytest.MonkeyPatch, **paths: str) -> None:
    """Stub :meth:`PromptTemplate.from_package_relative` for skill_selector paths used in production."""

    fakes = {path: PromptTemplate(content) for path, content in paths.items()}

    def _fake_from_pkg(_cls: type[PromptTemplate], path: str) -> PromptTemplate:
        return fakes[path]

    monkeypatch.setattr(
        planner_prompt_builder.PromptTemplate,
        "from_package_relative",
        classmethod(_fake_from_pkg),
    )


def _write_agent_yaml(tmp_path: Path, *, relevant_skills_limit: object) -> Path:
    cfg_path = tmp_path / "agent_relevant_skills_limit_st.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "AGENT_CONFIG": {
                    "name": "st_relevant_skills_limit",
                    "backend": "langgraph",
                    "type": "react",
                    "relevant_skills_limit": relevant_skills_limit,
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return cfg_path


def _runtime_from_merged_config(merged: dict, monkeypatch: pytest.MonkeyPatch, *, config_manager: Any):
    """与 Flex 一致的 Runtime；避免本 ST 依赖真实 MODEL provider 环境变量。"""
    monkeypatch.setattr(
        flex_rt,
        "build_llm_configs_from_flex_config",
        lambda _c: {
            "planner": {
                "model": "stub-model",
                "api_base": "http://127.0.0.1:9/v1",
                "api_key": "stub",
                "custom_llm_provider": "openai",
            }
        },
    )
    return flex_rt.build_runtime_from_flex_config(merged, mode="chat", config_manager=config_manager)


def _skill_selector_json_picking_first_builtin(runtime) -> str:
    skills = runtime.list_builtin_skills()
    assert skills, "ST 需要至少一个 builtin skill 才能走 skill-selector LLM 分支"
    name = str(skills[0].get("name") or "")
    assert name
    return json.dumps(
        {
            "include": [],
            "exclude": [],
            "ranked_candidates": [{"name": name, "score": 0.99, "reason": "st"}],
            "selected": [name],
        }
    )


def test_st_get_relevant_skills_limit_matches_yaml_int_after_data_agent_from_config(tmp_path: Path):
    """整型 limit：YAML → ``DataAgent.from_config`` → ``config_manager`` 与 planner 读取一致。"""
    cfg_path = _write_agent_yaml(tmp_path, relevant_skills_limit=7)
    agent = DataAgent.from_config(cfg_path)

    assert agent.config.get("AGENT_CONFIG.relevant_skills_limit") == 7

    merged = agent.config.get_all() or {}
    assert planner_prompt_builder._get_relevant_skills_limit(merged) == 7


def test_st_get_relevant_skills_limit_matches_yaml_string_after_data_agent_from_config(tmp_path: Path):
    """YAML 中数字字符串：加载后 ``_get_relevant_skills_limit`` 仍得到规范化整数。"""
    cfg_path = _write_agent_yaml(tmp_path, relevant_skills_limit="5")
    agent = DataAgent.from_config(cfg_path)

    merged = agent.config.get_all() or {}
    assert planner_prompt_builder._get_relevant_skills_limit(merged) == 5


def test_st_build_flex_skill_prompt_variables_zero_limit_no_skill_selector_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``relevant_skills_limit: 0`` 时走早退分支，不调 skill-selector LLM，且变量为空段落。"""
    cfg_path = _write_agent_yaml(tmp_path, relevant_skills_limit=0)
    agent = DataAgent.from_config(cfg_path)

    merged = agent.config.get_all() or {}
    assert planner_prompt_builder._get_relevant_skills_limit(merged) == 0

    runtime = _runtime_from_merged_config(merged, monkeypatch, config_manager=agent.config)
    out = planner_prompt_builder._build_flex_skill_prompt_variables(
        state={"session_id": "st-rs0", "run_id": 1, "user_query": "hello", "messages": []},
        runtime=runtime,
    )
    assert out["builtin_skills_prompt"] == ""
    assert out["user_skills_prompt"] == ""


def test_st_build_flex_skill_prompt_variables_skill_selector_sees_yaml_limit_with_flex_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """YAML limit>0 时，经真实 Runtime 走 ``_build_flex_skill_prompt_variables``，skill-selector 系统提示须含该 limit。

    若实现退化为仅用 ``runtime.env`` 拼 ``AGENT_CONFIG`` 且未带上 ``relevant_skills_limit``，
    则 ``relevant_skills_limit`` 变为 ``None``，不会进入本 LLM 分支（或系统提示不会出现 ``SYS limit=3``）。
    """
    cfg_path = _write_agent_yaml(tmp_path, relevant_skills_limit=3)
    agent = DataAgent.from_config(cfg_path)
    merged = agent.config.get_all() or {}
    assert planner_prompt_builder._get_relevant_skills_limit(merged) == 3

    runtime = _runtime_from_merged_config(merged, monkeypatch, config_manager=agent.config)

    _inject_skill_selector_prompts(
        monkeypatch,
        **{
            f"{PROMPT_MD_PREFIX}/skill_selector/system": "SYS limit={{ relevant_skills_limit }}",
            f"{PROMPT_MD_PREFIX}/skill_selector/user": (
                "history={{ history_user_messages }} USER query={{ user_query }} skills={{ skills_json }}"
            ),
        },
    )

    llm = _FakeChatModel(_skill_selector_json_picking_first_builtin(runtime))
    monkeypatch.setattr(planner_prompt_builder.llm_manager, "get_default_llm", lambda: _default_llm(llm))
    monkeypatch.setattr(builtins, "breakpoint", lambda: None)

    planner_prompt_builder._build_flex_skill_prompt_variables(
        state={"session_id": "st-rs-pos", "run_id": 1, "user_query": "need skills", "messages": []},
        runtime=runtime,
    )

    assert llm.calls >= 1
    assert llm.last_messages is not None
    assert llm.last_messages[0].content == "SYS limit=3"
