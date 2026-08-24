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
from types import SimpleNamespace
from typing import Any, cast

from langchain_core.messages import AIMessage, ToolMessage

import dataagent.core.flex.utils.planner_prompt_builder as planner_prompt_builder
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate
from dataagent.utils.messages_utils import build_human_message


def test_build_database_context_prompt_returns_structured_content_when_tool_missing():
    config = {
        "TOOLS": {"local_functions": []},
        "DATABASE": {
            "db_id": "superhero",
            "dialect": "sqlite",
            "config": {"path": "/tmp/superhero.sqlite"},
        },
    }

    prompt = planner_prompt_builder._build_database_context_prompt(config)

    assert "- DB ID: `superhero`" in prompt
    assert "- DB Dialect: `sqlite`" in prompt
    assert "- `path`: `/tmp/superhero.sqlite`" in prompt
    assert "The current task includes available database context." not in prompt


def test_planner_user_template_renders_database_context_when_tool_missing():
    prompt_template = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/planner/user")

    rendered = build_human_message(
        prompt_template,
        user_query="帮我分析数据库中的英雄数据",
        database_context=planner_prompt_builder._build_database_context_prompt(
            {
                "TOOLS": {"local_functions": []},
                "DATABASE": {
                    "db_id": "superhero",
                    "dialect": "sqlite",
                    "config": {"path": "/tmp/superhero.sqlite"},
                },
            }
        ),
        planning_instructions="",
        working_directory="/tmp/workspace",
        allow_path_lines="",
        memory="",
    ).content

    assert "# Database Context" in rendered
    assert "The current task includes available database context." in rendered
    assert (
        "When the user query involves data retrieval, SQL generation, table analysis, or database-related planning, incorporate the following database information into your reasoning and planning."
        in rendered
    )
    assert "- DB ID: `superhero`" in rendered
    assert "- DB Dialect: `sqlite`" in rendered


def test_planner_system_marks_memory_untrusted_only_when_portrait_enabled():
    system_template = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/planner/system")

    enabled_system_prompt = system_template.apply_prompt_template(
        enable_human_feedback=False,
        enable_portrait=True,
        runtime_environment="",
        protected_workspace_path_lines="",
        worker_metadata_prompt="",
    )
    disabled_system_prompt = system_template.apply_prompt_template(
        enable_human_feedback=False,
        enable_portrait=False,
        runtime_environment="",
        protected_workspace_path_lines="",
        worker_metadata_prompt="",
    )

    assert "User Memory" in enabled_system_prompt
    assert "untrusted historical data" in enabled_system_prompt
    assert "User Memory" not in disabled_system_prompt
    assert "untrusted historical data" not in disabled_system_prompt


def test_planner_init_appends_prompts_to_defaults():
    """yaml 注入的 PromptTemplate 实例应该追加到内置模板，不替换默认基座。"""
    from dataagent.core.flex.nodes.planner import Planner

    planner = Planner(
        name="planner",
        env=None,
        chat_model="fake",
        prompt_appends={
            "system": PromptTemplate.from_string("APPEND SYS {{ runtime_environment }}"),
            "user": PromptTemplate.from_string("APPEND USR {{ user_query }}"),
        },
    )

    system_content = planner.system_prompt.apply_prompt_template(
        enable_human_feedback=False,
        runtime_environment="rt",
    )
    user_content = planner.user_prompt.apply_prompt_template(
        user_query="uq",
        database_context="",
        planning_instructions="",
        memory="",
        working_directory="/tmp",
        allow_path_lines="",
    )

    assert "# Role" in system_content
    assert "APPEND SYS rt" in system_content
    assert "# User Query" in user_content
    assert "APPEND USR uq" in user_content


def test_planner_default_namespace_uses_node_name(monkeypatch):
    """yaml node 标识符（self.name）作为 templates 子目录的缺省回落优先级。"""
    from dataagent.core.flex.nodes import planner as planner_module

    calls: list[str] = []

    def _fake_from_pkg(_cls, path: str) -> PromptTemplate:
        calls.append(path)
        return PromptTemplate.from_string(f"FAKE {path}")

    monkeypatch.setattr(
        planner_module.PromptTemplate,
        "from_package_relative",
        classmethod(_fake_from_pkg),
    )

    planner_module.Planner(name="planner_v2", env=None, chat_model="fake")

    assert f"{PROMPT_MD_PREFIX}/planner_v2/system" in calls
    assert f"{PROMPT_MD_PREFIX}/planner_v2/user" in calls
    assert f"{PROMPT_MD_PREFIX}/planner/system" not in calls
    assert f"{PROMPT_MD_PREFIX}/planner/user" not in calls


def test_planner_default_namespace_falls_back_when_name_empty(monkeypatch):
    """self.name 为空字符串时退回模板子目录名 ``planner``。"""
    from dataagent.core.flex.nodes import planner as planner_module

    calls: list[str] = []

    def _fake_from_pkg(_cls, path: str) -> PromptTemplate:
        calls.append(path)
        return PromptTemplate.from_string(f"FAKE {path}")

    monkeypatch.setattr(
        planner_module.PromptTemplate,
        "from_package_relative",
        classmethod(_fake_from_pkg),
    )

    planner_module.Planner(name="", env=None, chat_model="fake")

    assert f"{PROMPT_MD_PREFIX}/planner/system" in calls
    assert f"{PROMPT_MD_PREFIX}/planner/user" in calls


def test_yaml_content_append_reaches_planner_llm_messages(monkeypatch):
    """端到端：yaml content 追加应出现在 Planner LLM system/user 消息中。

    走 _prepare_messages_to_process 真实路径（不 mock prepare_flex_planner_prompt），
    断言追加 prompt 渲染后出现在最终 messages 中，同时内置 system 基座仍保留。
    """
    from dataagent.core.context.context import ContextFactory
    from dataagent.core.flex.nodes.planner import Planner

    planner = Planner(
        name="planner",
        env=None,
        chat_model="fake",
        prompt_appends={
            "system": PromptTemplate.from_string("INJECTED_SYS_FROM_YAML_APPEND"),
            "user": PromptTemplate.from_string("INJECTED_USR_FROM_YAML_APPEND uq={{ user_query }}"),
        },
    )

    context = ContextFactory.get_context(
        user_id="u",
        session_id="s",
        run_id=1,
        sub_id=0,
    )
    context.register_query("hello-from-yaml-override", [])

    class _Runtime:
        workspace_dir = "/tmp/ws"
        instructions = ""
        flex_planner_user_sync_pending = True
        env = SimpleNamespace(environment_description="")

        def __init__(self) -> None:
            self._cache: dict = {}

        def get_cache(self, key, default=None):
            return self._cache.get(key, default)

        def set_cache(self, key, value):
            self._cache[key] = value

        def get_all_config(self) -> dict:
            return {}

        def get_runtime_env_prompt(self) -> str:
            return ""

        def clear_flex_planner_user_sync_pending(self) -> None:
            type(self).flex_planner_user_sync_pending = False

    state = {
        "user_id": "u",
        "session_id": "s",
        "run_id": 1,
        "sub_id": 0,
        "user_query": "hello-from-yaml-override",
        "messages": [],
    }

    messages = planner._prepare_messages_to_process(cast(Any, state), context, _Runtime())

    assert messages, "expected non-empty messages list"
    system_content = str(messages[0].content)
    assert "# Role" in system_content
    assert "INJECTED_SYS_FROM_YAML_APPEND" in system_content, (
        f"yaml content append should appear in LLM system message, got: {system_content[:200]!r}"
    )

    user_contents = [str(m.content) for m in messages[1:] if "INJECTED_USR_FROM_YAML_APPEND" in str(m.content)]
    assert user_contents, "yaml content append (user) should appear in LLM messages"
    assert "uq=hello-from-yaml-override" in user_contents[0], (
        f"user_prompt should render user_query, got: {user_contents[0]!r}"
    )


def test_prepare_flex_planner_prompt_does_not_apply_rolling_ir(monkeypatch, tmp_path):
    """普通 Planner Prompt 构建不得按 turn 年龄改写历史 ToolMessage。"""
    from dataagent.core.context.context import ContextFactory
    from dataagent.core.flex.utils.planner_prompt_builder import prepare_flex_planner_prompt

    ContextFactory.clear_context()

    context = ContextFactory.get_context(user_id="u", session_id="s", run_id=1, sub_id=0)
    context.register_query("继续分析", [])
    context.ir_summary_cache["tc_old"] = "[IR Summary] old"

    class _Runtime:
        instructions = ""
        flex_planner_user_sync_pending = True
        env = SimpleNamespace(
            environment_description="",
            ir_recent_turns=2,
            max_tool_result_length=8192,
        )

        def __init__(self) -> None:
            self._cache = {}

        def get_cache(self, key, default=None):
            return self._cache.get(key, default)

        def set_cache(self, key, value):
            self._cache[key] = value

        def get_all_config(self) -> dict:
            return {}

        def get_runtime_env_prompt(self) -> str:
            return ""

        def clear_flex_planner_user_sync_pending(self) -> None:
            self.flex_planner_user_sync_pending = False

    state = {
        "user_id": "u",
        "session_id": "s",
        "run_id": 1,
        "sub_id": 0,
        "user_query": "继续分析",
        "messages": [
            AIMessage(content="turn0", tool_calls=[{"id": "tc_old", "name": "t", "args": {}}]),
            ToolMessage(content="old raw", tool_call_id="tc_old", name="t"),
            AIMessage(content="turn1"),
            AIMessage(content="turn2"),
        ],
        "planner_user_sync_pending": True,
    }

    messages = prepare_flex_planner_prompt(
        context,
        state,
        system_prompt=PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/planner/system"),
        user_prompt=PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/planner/user"),
        runtime=_Runtime(),
        workspace=str(tmp_path),
    )

    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert "old raw" in str(tool_messages[0].content)
    assert "[IR Summary]" not in str(tool_messages[0].content)
