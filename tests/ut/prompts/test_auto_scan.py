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
"""from_package_relative 路径解析与异常契约的 smoke 测试。

历史上这里测试的是已删除的 PromptManager 注册接口（register_prompt / delete_prompt /
get_node_prompts 等），新方案下注册能力下沉为：
- 默认模板：放在 ``dataagent`` 包内 ``templates/<...>.md``，由 ``PromptTemplate.from_package_relative`` 读取
- 运行时覆盖：由 ``dataagent.config.config_manager.build_prompt`` 按需构造 ``PromptTemplate``
所以本文件只验证包根相对路径与缺失时的 ``ValueError``。
"""

import pytest
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate

from dataagent.agents.nl2sql.nodes.base_nl2sql_node import NL2SQL_PROMPT_PREFIX


def test_from_package_relative_returns_prompt_template_for_existing_path():
    tmpl = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/planner/system")
    assert isinstance(tmpl, PromptTemplate)
    assert "# Role" in tmpl.content
    assert "DataAgent" in tmpl.content


def test_from_package_relative_supports_multi_level_path():
    tmpl = PromptTemplate.from_package_relative(f"{NL2SQL_PROMPT_PREFIX}/generator/prompt_system")
    assert isinstance(tmpl, PromptTemplate)
    assert "num_samples" in tmpl.content


def test_from_package_relative_raises_value_error_on_missing():
    with pytest.raises(ValueError, match="not found"):
        PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/__no_such_namespace__/__no_such_message__")


def test_build_prompt_rejects_relative_path():
    from dataagent.config.config_manager import build_prompt

    with pytest.raises(ValueError, match="absolute"):
        build_prompt({"path": "prompts/relative_only.md"})


def test_build_prompt_reads_file_with_absolute_path(tmp_path):
    from dataagent.config.config_manager import build_prompt

    p = tmp_path / "p.md"
    p.write_text("hello {{ x }}", encoding="utf-8")
    tmpl = build_prompt({"path": str(p.resolve())})
    assert tmpl.apply_prompt_template(x="w") == "hello w"


def test_flex_prompt_template_appends_to_planner_defaults():
    from dataagent.core.flex.agent import FlexAgent

    nodes = FlexAgent._create_nodes_from_config(
        [
            {
                "node": "planner",
                "module": "dataagent.core.flex.nodes.planner.Planner",
                "chat_model": {"name": "test-model"},
                "prompt_template": {
                    "system": {"content": "custom system {{ runtime_environment }}"},
                    "user": {"content": "custom user {{ user_query }}"},
                },
            }
        ]
    )

    planner = nodes[0]
    assert "# Role" in planner.system_prompt.content
    assert "DataAgent" in planner.system_prompt.content
    system_rendered = planner.system_prompt.apply_prompt_template(
        builtin_skills_prompt="",
        user_skills_prompt="",
        enable_human_feedback=False,
        runtime_environment="runtime",
    )
    user_rendered = planner.user_prompt.apply_prompt_template(
        user_query="question",
        database_context="",
        planning_instructions="",
        memory="",
        working_directory="/tmp",
        allow_path_lines="",
    )
    assert "custom system runtime" in system_rendered
    assert "custom user question" in user_rendered
