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
import builtins
import json
from types import SimpleNamespace
from typing import Any, cast

import dataagent.core.flex.utils.planner_prompt_builder as planner_prompt_builder
import dataagent.utils.messages_utils as messages_utils
from dataagent.core.managers.llm_manager.adapters import ChatModel, LLMResponse
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate
from dataagent.utils.messages_utils import build_ai_message, build_human_message, build_system_message


class _FakeChatModel:
    def __init__(self, response_content: str):
        self._response_content = response_content
        self.calls = 0
        self.last_messages = None

    def invoke(self, messages, **_kwargs):
        self.calls += 1
        self.last_messages = messages
        return LLMResponse(content=self._response_content, usage_metadata={})


class _FakeRuntime:
    def __init__(self, llm=None, builtin_skills=None, user_skills=None, agent_config=None):
        self._cache = {}
        self._llm = llm
        self._builtin_skills = builtin_skills or []
        self._user_skills = user_skills or []
        self._agent_config = dict(agent_config or {})

    def get_cache(self, key: str, default=None):
        return self._cache.get(key, default)

    def set_cache(self, key: str, value):
        self._cache[key] = value

    def get_all_config(self) -> dict:
        import copy

        return copy.deepcopy(self._agent_config)

    def get_config(self, key: str, default=None):
        cur: Any = self._agent_config
        for part in key.split("."):
            if not isinstance(cur, dict):
                return default
            cur = cur.get(part)
            if cur is None:
                return default
        return cur

    def llm(self, _name: str):
        if self._llm is None:
            raise AssertionError("llm should not be called in this test")
        return self._llm

    def list_builtin_skills(self):
        return self._builtin_skills

    def list_user_skills(self):
        return self._user_skills


def _default_llm(llm: _FakeChatModel) -> ChatModel:
    return cast(ChatModel, llm)


def _select_relevant_skills_for_prompt(**kwargs):
    if "user_query" in kwargs and "latest_user_query" not in kwargs:
        kwargs["latest_user_query"] = kwargs.pop("user_query")
    if "relevant_skills_limit" in kwargs:
        kwargs["relevant_skills_limit"] = planner_prompt_builder._normalize_relevant_skills_limit(
            kwargs["relevant_skills_limit"]
        )
    return planner_prompt_builder._select_relevant_skills_for_prompt(**kwargs)


def _build_flex_skill_prompt_variables(**kwargs):
    return planner_prompt_builder._build_flex_skill_prompt_variables(**kwargs)


def _inject_prompts(monkeypatch, **paths):
    """注入 :meth:`PromptTemplate.from_package_relative` 的 fake 返回值。

    字典的键须与生产侧路径一致（``dataagent`` 包根相对路径，常写 ``f"{PROMPT_MD_PREFIX}/…"``）。
    """
    fakes = {}
    for path, content in paths.items():
        fakes[path] = PromptTemplate(content)

    def _fake_from_pkg(_cls, path: str) -> PromptTemplate:
        return fakes[path]

    monkeypatch.setattr(
        planner_prompt_builder.PromptTemplate,
        "from_package_relative",
        classmethod(_fake_from_pkg),
    )


def test_build_database_context_prompt_returns_empty_when_nl2sql_tool_exists():
    config = {
        "TOOLS": {
            "local_functions": [
                {
                    "module": "dataagent.actions.tools.local_tool.tools",
                    "function": "nl2sql_sub_agent_tool",
                }
            ]
        },
        "DATABASE": {
            "db_id": "superhero",
            "engine": "sqlite",
            "config": {"path": "/tmp/superhero.sqlite"},
        },
    }

    assert planner_prompt_builder._build_database_context_prompt(config) == ""


def test_build_database_context_prompt_returns_structured_content_when_tool_missing():
    config = {
        "TOOLS": {"local_functions": []},
        "DATABASE": {
            "db_id": "superhero",
            "engine": "sqlite",
            "config": {"path": "/tmp/superhero.sqlite"},
        },
    }

    prompt = planner_prompt_builder._build_database_context_prompt(config)

    assert "- DB ID: `superhero`" in prompt
    assert "- DB Engine: `sqlite`" in prompt
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
                    "engine": "sqlite",
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
    assert "- DB Engine: `sqlite`" in rendered


def test_planner_user_template_skips_database_context_when_nl2sql_tool_exists():
    prompt_template = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/planner/user")

    rendered = build_human_message(
        prompt_template,
        user_query="帮我分析数据库中的英雄数据",
        database_context=planner_prompt_builder._build_database_context_prompt(
            {
                "TOOLS": {
                    "local_functions": [
                        {
                            "module": "dataagent.actions.tools.local_tool.tools",
                            "function": "nl2sql_sub_agent_tool",
                        }
                    ]
                },
                "DATABASE": {
                    "db_id": "superhero",
                    "engine": "sqlite",
                    "config": {"path": "/tmp/superhero.sqlite"},
                },
            }
        ),
        planning_instructions="",
        working_directory="/tmp/workspace",
        allow_path_lines="",
        memory="",
    ).content

    assert "# Database Context" not in rendered
    assert "superhero" not in rendered


def test_skill_selector_system_template_includes_standard_agent_elements():
    prompt_template = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/skill_selector/system")

    rendered = build_system_message(prompt_template, relevant_skills_limit=2).content

    assert "# Role" in rendered
    assert "# Task" in rendered
    assert "# Rules" in rendered
    assert "# Boundaries" in rendered
    assert "# Tools" in rendered
    assert "# Reasoning Pattern" in rendered
    assert "# Output Contract" in rendered
    assert "# Few-shot Examples" in rendered
    assert '"selected": [' in rendered
    assert '"excel"' in rendered
    assert '"selected": []' in rendered


def test_normalize_relevant_skills_limit_no_warning_when_unset():
    assert planner_prompt_builder._normalize_relevant_skills_limit(None) is None
    assert planner_prompt_builder._normalize_relevant_skills_limit("") is None
    assert planner_prompt_builder._normalize_relevant_skills_limit("  \t  ") is None


def test_normalize_relevant_skills_limit_warns_on_invalid_types(monkeypatch):
    warnings: list[str] = []

    def _capture(msg: str, *args, **kwargs):
        warnings.append(msg)

    monkeypatch.setattr(planner_prompt_builder.logger, "warning", _capture)
    assert planner_prompt_builder._normalize_relevant_skills_limit([]) is None
    assert planner_prompt_builder._normalize_relevant_skills_limit({}) is None
    assert planner_prompt_builder._normalize_relevant_skills_limit(1.5) is None
    assert planner_prompt_builder._normalize_relevant_skills_limit(True) is None
    assert planner_prompt_builder._normalize_relevant_skills_limit(-3) is None
    assert planner_prompt_builder._normalize_relevant_skills_limit("x") is None
    assert planner_prompt_builder._normalize_relevant_skills_limit("-2") is None
    assert len(warnings) == 7
    assert all("relevant_skills_limit is invalid" in m for m in warnings)


def test_normalize_relevant_skills_limit_accepts_non_negative_int_and_digit_string():
    assert planner_prompt_builder._normalize_relevant_skills_limit(0) == 0
    assert planner_prompt_builder._normalize_relevant_skills_limit(42) == 42
    assert planner_prompt_builder._normalize_relevant_skills_limit("0") == 0
    assert planner_prompt_builder._normalize_relevant_skills_limit("  7  ") == 7


def test_select_relevant_skills_returns_all_candidates_when_limit_disabled():
    builtin_skills = [{"name": "builtin_a", "description": "builtin a"}]
    user_skills = [{"name": "user_a", "description": "user a"}]

    result = _select_relevant_skills_for_prompt(
        user_query="analyze data",
        builtin_skills=builtin_skills,
        user_skills=user_skills,
        runtime=_FakeRuntime(),
        relevant_skills_limit=None,
    )

    assert result["selected_builtin_skills"] == builtin_skills
    assert result["selected_user_skills"] == user_skills
    assert result["selection_debug_info"]["mode"] == "all"


def test_select_relevant_skills_treats_negative_or_invalid_limit_as_disabled():
    builtin_skills = [{"name": "builtin_a", "description": "builtin a"}]
    user_skills = [{"name": "user_a", "description": "user a"}]

    for raw_limit in (-1, "-1", "invalid", ""):
        result = _select_relevant_skills_for_prompt(
            user_query="analyze data",
            builtin_skills=builtin_skills,
            user_skills=user_skills,
            runtime=_FakeRuntime(),
            relevant_skills_limit=raw_limit,
        )
        assert result["selected_builtin_skills"] == builtin_skills
        assert result["selected_user_skills"] == user_skills
        assert result["selection_debug_info"]["mode"] == "all"


def test_select_relevant_skills_zero_limit_selects_no_skills():
    result = _select_relevant_skills_for_prompt(
        user_query="analyze data",
        builtin_skills=[{"name": "builtin_a", "description": "builtin a"}],
        user_skills=[{"name": "user_a", "description": "user a"}],
        runtime=_FakeRuntime(),
        relevant_skills_limit=0,
    )
    assert result["selected_builtin_skills"] == []
    assert result["selected_user_skills"] == []
    assert result["selection_debug_info"]["mode"] == "none"


def test_select_relevant_skills_string_zero_limit_selects_no_skills():
    result = _select_relevant_skills_for_prompt(
        user_query="analyze data",
        builtin_skills=[{"name": "builtin_a", "description": "builtin a"}],
        user_skills=[{"name": "user_a", "description": "user a"}],
        runtime=_FakeRuntime(),
        relevant_skills_limit="0",
    )
    assert result["selected_builtin_skills"] == []
    assert result["selected_user_skills"] == []
    assert result["selection_debug_info"]["mode"] == "none"


def test_select_relevant_skills_accepts_string_positive_limit(monkeypatch):
    llm = _FakeChatModel(
        json.dumps(
            {
                "include": [],
                "exclude": [],
                "ranked_candidates": [{"name": "builtin_a", "score": 0.91, "reason": "direct match"}],
                "selected": ["builtin_a"],
            }
        )
    )
    monkeypatch.setattr(planner_prompt_builder.llm_manager, "get_default_llm", lambda: _default_llm(llm))
    monkeypatch.setattr(builtins, "breakpoint", lambda: None)

    result = _select_relevant_skills_for_prompt(
        user_query="analyze data",
        builtin_skills=[{"name": "builtin_a", "description": "builtin a"}],
        user_skills=[{"name": "user_a", "description": "user a"}],
        runtime=_FakeRuntime(),
        relevant_skills_limit="1",
    )

    assert [skill["name"] for skill in result["selected_builtin_skills"]] == ["builtin_a"]
    assert result["selection_debug_info"]["mode"] == "model"


def test_select_relevant_skills_uses_ranked_candidates_and_runtime_cache():
    builtin_skills = [
        {"name": "builtin_a", "description": "builtin a"},
        {"name": "builtin_b", "description": "builtin b"},
    ]
    user_skills = [
        {"name": "user_a", "description": "user a"},
        {"name": "user_b", "description": "user b"},
    ]
    llm = _FakeChatModel(
        json.dumps(
            {
                "include": [{"name": "builtin_b", "reason": "must use"}],
                "exclude": [{"name": "user_b", "reason": "do not use"}],
                "ranked_candidates": [
                    {"name": "builtin_b", "score": 0.99, "reason": "best match"},
                    {"name": "user_a", "score": 0.86, "reason": "good match"},
                    {"name": "builtin_a", "score": 0.42, "reason": "weak match"},
                ],
                "selected": ["builtin_a"],
            }
        )
    )
    runtime = _FakeRuntime(llm=llm)
    original_get_default_llm = planner_prompt_builder.llm_manager.get_default_llm
    original_breakpoint = builtins.breakpoint
    planner_prompt_builder.llm_manager.get_default_llm = lambda: _default_llm(llm)
    builtins.breakpoint = lambda: None

    try:
        result_first = _select_relevant_skills_for_prompt(
            user_query="analyze data",
            builtin_skills=builtin_skills,
            user_skills=user_skills,
            runtime=runtime,
            relevant_skills_limit=2,
        )
        result_second = _select_relevant_skills_for_prompt(
            user_query="analyze data",
            builtin_skills=builtin_skills,
            user_skills=user_skills,
            runtime=runtime,
            relevant_skills_limit=2,
        )
    finally:
        planner_prompt_builder.llm_manager.get_default_llm = original_get_default_llm
        builtins.breakpoint = original_breakpoint

    assert [skill["name"] for skill in result_first["selected_builtin_skills"]] == ["builtin_b"]
    assert [skill["name"] for skill in result_first["selected_user_skills"]] == ["user_a"]
    assert result_first == result_second
    assert llm.calls == 1
    assert result_first["selection_debug_info"]["mode"] == "model"


def test_flex_skill_selector_prompt_uses_human_history_and_latest_query(monkeypatch):
    _inject_prompts(
        monkeypatch,
        **{
            f"{PROMPT_MD_PREFIX}/skill_selector/system": "SYS limit={{ relevant_skills_limit }}",
            f"{PROMPT_MD_PREFIX}/skill_selector/user": "history={{ history_user_messages }} latest={{ user_query }} skills={{ skills_json }}",
        },
    )
    monkeypatch.setattr(builtins, "breakpoint", lambda: None)
    llm = _FakeChatModel(
        json.dumps(
            {
                "include": [{"name": "skillA", "reason": "requested earlier"}],
                "exclude": [],
                "ranked_candidates": [{"name": "skillA", "score": 0.95, "reason": "requested by user"}],
                "selected": ["skillA"],
            }
        )
    )
    monkeypatch.setattr(planner_prompt_builder.llm_manager, "get_default_llm", lambda: _default_llm(llm))
    state = {
        "session_id": "session-1",
        "run_id": 2,
        "user_query": "可以",
        "messages": [
            build_human_message(prompt_str="我要用 skillA 处理这个任务"),
            build_ai_message("I will use skillB"),
            messages_utils.ToolMessage(content="tool result mentions skillB", tool_call_id="tool-1"),
        ],
    }

    _build_flex_skill_prompt_variables(
        state=state,
        runtime=_FakeRuntime(
            builtin_skills=[{"name": "skillA", "description": "skill A"}],
            user_skills=[{"name": "skillB", "description": "skill B"}],
            agent_config={"AGENT_CONFIG": {"relevant_skills_limit": 1}},
        ),
    )

    assert llm.last_messages is not None
    user_prompt = llm.last_messages[1].content
    assert "我要用 skillA 处理这个任务" in user_prompt
    assert "latest=可以" in user_prompt
    assert "I will use skillB" not in user_prompt
    assert "tool result mentions skillB" not in user_prompt


def test_flex_skill_selector_history_extracts_user_query_from_planner_human_messages(monkeypatch):
    _inject_prompts(
        monkeypatch,
        **{
            f"{PROMPT_MD_PREFIX}/skill_selector/system": "SYS limit={{ relevant_skills_limit }}",
            f"{PROMPT_MD_PREFIX}/skill_selector/user": "history={{ history_user_messages }} latest={{ user_query }} skills={{ skills_json }}",
        },
    )
    monkeypatch.setattr(builtins, "breakpoint", lambda: None)
    llm = _FakeChatModel(
        json.dumps(
            {
                "include": [{"name": "skillA", "reason": "requested earlier"}],
                "exclude": [],
                "ranked_candidates": [{"name": "skillA", "score": 0.95, "reason": "requested by user"}],
                "selected": ["skillA"],
            }
        )
    )
    monkeypatch.setattr(planner_prompt_builder.llm_manager, "get_default_llm", lambda: _default_llm(llm))
    templated_human = build_human_message(
        prompt_str=(
            "# User Query\n"
            "<user_query>我要用 skillA 处理这个任务。</user_query>\n\n"
            "# Task Constraints\n"
            "Do not leak this section into skill selection history.\n\n"
            "# Working Directory\n"
            "<working_directory>/tmp/workspace</working_directory>"
        )
    )
    state = {
        "session_id": "session-1",
        "run_id": 2,
        "user_query": "可以。",
        "messages": [templated_human, templated_human],
    }

    _build_flex_skill_prompt_variables(
        state=state,
        runtime=_FakeRuntime(
            builtin_skills=[{"name": "skillA", "description": "skill A"}],
            user_skills=[],
            agent_config={"AGENT_CONFIG": {"relevant_skills_limit": 1}},
        ),
    )

    assert llm.last_messages is not None
    user_prompt = llm.last_messages[1].content
    assert "1. 我要用 skillA 处理这个任务。" in user_prompt
    assert "2. 我要用 skillA 处理这个任务。" not in user_prompt
    assert "# Task Constraints" not in user_prompt
    assert "<working_directory>" not in user_prompt
    assert "latest=可以。" in user_prompt


def test_flex_skill_selection_cache_key_is_scoped_by_session_and_run(monkeypatch):
    _inject_prompts(
        monkeypatch,
        **{
            f"{PROMPT_MD_PREFIX}/skill_selector/system": "SYS limit={{ relevant_skills_limit }}",
            f"{PROMPT_MD_PREFIX}/skill_selector/user": "USER query={{ user_query }}",
        },
    )
    monkeypatch.setattr(builtins, "breakpoint", lambda: None)
    llm = _FakeChatModel(
        json.dumps(
            {
                "include": [],
                "exclude": [],
                "ranked_candidates": [{"name": "skillA", "score": 0.95, "reason": "relevant"}],
                "selected": ["skillA"],
            }
        )
    )
    monkeypatch.setattr(planner_prompt_builder.llm_manager, "get_default_llm", lambda: _default_llm(llm))
    runtime = _FakeRuntime(
        builtin_skills=[{"name": "skillA", "description": "skill A"}],
        user_skills=[],
        agent_config={"AGENT_CONFIG": {"relevant_skills_limit": 1}},
    )
    state = {"session_id": "session-1", "run_id": 1, "user_query": "用 skillA", "messages": []}

    _build_flex_skill_prompt_variables(state=state, runtime=runtime)
    _build_flex_skill_prompt_variables(state=state, runtime=runtime)
    state["run_id"] = 2
    _build_flex_skill_prompt_variables(state=state, runtime=runtime)

    assert llm.calls == 2
    assert "planner_skill_selection:session-1:1" in runtime._cache
    assert "planner_skill_selection:session-1:2" in runtime._cache


def test_select_relevant_skills_warns_and_falls_back_to_all_skills(monkeypatch):
    builtin_skills = [
        {"name": "builtin_a", "description": "builtin a"},
        {"name": "builtin_b", "description": "builtin b"},
    ]
    user_skills = [{"name": "user_a", "description": "user a"}]
    warnings = []
    llm = _FakeChatModel("not-json")

    def _record_warning(message: str):
        warnings.append(message)

    module_logger = planner_prompt_builder.logger
    monkeypatch.setattr(module_logger, "warning", _record_warning)
    monkeypatch.setattr(planner_prompt_builder.llm_manager, "get_default_llm", lambda: _default_llm(llm))

    result = _select_relevant_skills_for_prompt(
        user_query="analyze data",
        builtin_skills=builtin_skills,
        user_skills=user_skills,
        runtime=_FakeRuntime(llm=llm),
        relevant_skills_limit=2,
    )

    assert [skill["name"] for skill in result["selected_builtin_skills"]] == ["builtin_a", "builtin_b"]
    assert [skill["name"] for skill in result["selected_user_skills"]] == ["user_a"]
    assert result["selection_debug_info"]["mode"] == "fallback"
    assert warnings


def test_select_relevant_skills_returns_empty_when_model_selects_no_skills():
    llm = _FakeChatModel(
        json.dumps(
            {
                "include": [],
                "exclude": [
                    {"name": "builtin_a", "reason": "user explicitly forbids all skills"},
                    {"name": "user_a", "reason": "user explicitly forbids all skills"},
                ],
                "ranked_candidates": [],
                "selected": [],
            }
        )
    )
    original_get_default_llm = planner_prompt_builder.llm_manager.get_default_llm
    original_breakpoint = builtins.breakpoint
    planner_prompt_builder.llm_manager.get_default_llm = lambda: _default_llm(llm)
    builtins.breakpoint = lambda: None

    try:
        result = _select_relevant_skills_for_prompt(
            user_query="不要使用任何skills",
            builtin_skills=[{"name": "builtin_a", "description": "builtin a"}],
            user_skills=[{"name": "user_a", "description": "user a"}],
            runtime=_FakeRuntime(),
            relevant_skills_limit=2,
        )
    finally:
        planner_prompt_builder.llm_manager.get_default_llm = original_get_default_llm
        builtins.breakpoint = original_breakpoint

    assert result["selected_builtin_skills"] == []
    assert result["selected_user_skills"] == []
    assert result["selection_debug_info"]["mode"] == "model"
    assert llm.calls == 1


def test_select_relevant_skills_falls_back_when_schema_invalid(monkeypatch):
    warnings = []

    def _record_warning(message: str):
        warnings.append(message)

    monkeypatch.setattr(planner_prompt_builder.logger, "debug", _record_warning)
    llm = _FakeChatModel(
        json.dumps(
            {
                "include": ["builtin_b"],
                "exclude": [],
                "ranked_candidates": [{"name": "builtin_b", "reason": "missing score"}],
                "selected": ["builtin_b"],
            }
        )
    )
    original_get_default_llm = planner_prompt_builder.llm_manager.get_default_llm
    original_breakpoint = builtins.breakpoint
    planner_prompt_builder.llm_manager.get_default_llm = lambda: _default_llm(llm)
    builtins.breakpoint = lambda: None

    try:
        result = _select_relevant_skills_for_prompt(
            user_query="analyze data",
            builtin_skills=[
                {"name": "builtin_a", "description": "builtin a"},
                {"name": "builtin_b", "description": "builtin b"},
            ],
            user_skills=[{"name": "user_a", "description": "user a"}],
            runtime=_FakeRuntime(),
            relevant_skills_limit=2,
        )
    finally:
        planner_prompt_builder.llm_manager.get_default_llm = original_get_default_llm
        builtins.breakpoint = original_breakpoint

    assert [skill["name"] for skill in result["selected_builtin_skills"]] == ["builtin_a", "builtin_b"]
    assert [skill["name"] for skill in result["selected_user_skills"]] == ["user_a"]
    assert result["selection_debug_info"]["mode"] == "fallback"
    assert warnings


def test_select_relevant_skills_uses_chat_model_and_template_prompts(monkeypatch):
    _inject_prompts(
        monkeypatch,
        **{
            f"{PROMPT_MD_PREFIX}/skill_selector/system": "SYS limit={{ relevant_skills_limit }}",
            f"{PROMPT_MD_PREFIX}/skill_selector/user": "history={{ history_user_messages }} USER query={{ user_query }} skills={{ skills_json }}",
        },
    )

    llm = _FakeChatModel(
        json.dumps(
            {
                "include": [],
                "exclude": [],
                "ranked_candidates": [{"name": "builtin_a", "score": 0.9, "reason": "best match"}],
                "selected": ["builtin_a"],
            }
        )
    )
    requested_names = []

    def _fake_get_default_llm():
        requested_names.append("chat_model")
        return llm

    monkeypatch.setattr(planner_prompt_builder.llm_manager, "get_default_llm", _fake_get_default_llm)
    monkeypatch.setattr(builtins, "breakpoint", lambda: None)

    result = _select_relevant_skills_for_prompt(
        user_query="analyze data",
        builtin_skills=[{"name": "builtin_a", "description": "builtin a"}],
        user_skills=[{"name": "user_a", "description": "user a"}],
        runtime=_FakeRuntime(),
        relevant_skills_limit=1,
    )

    assert requested_names == ["chat_model"]
    assert llm.last_messages is not None
    assert llm.last_messages[0].content == "SYS limit=1"
    assert "USER query=analyze data" in llm.last_messages[1].content
    assert "history=" in llm.last_messages[1].content
    assert '"name": "builtin_a"' in llm.last_messages[1].content
    assert result["selection_debug_info"]["mode"] == "model"


# ---------------------------------------------------------------------------
# 回归：yaml prompt_template 追加 → Planner 持有 → LLM 入参 的端到端数据流
# ---------------------------------------------------------------------------


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
        builtin_skills_prompt="",
        user_skills_prompt="",
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
    from dataagent.core.flex.nodes.planner import Planner

    from dataagent.core.context.context_trajectory import ContextFactory

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

        def list_builtin_skills(self):
            return []

        def list_user_skills(self):
            return []

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


def test_prepare_flex_planner_prompt_injects_worker_metadata_into_system(monkeypatch, tmp_path):
    from dataagent.core.flex.utils.planner_prompt_builder import prepare_flex_planner_prompt

    from dataagent.core.context.context_trajectory import ContextFactory
    from dataagent.core.swarm.worker_metadata import upsert_worker_metadata

    ContextFactory.clear_context()
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))
    monkeypatch.setattr(planner_prompt_builder, "swarm_enabled", lambda _cfg=None: True)
    monkeypatch.setattr(planner_prompt_builder.llm_manager, "get_default_llm", lambda: None)

    upsert_worker_metadata(
        user_id="u",
        parent_session_id="s",
        worker_session_id="subagent_s_123456",
        sub_id=123456,
        config_path="/tmp/sub.yaml",
        query="分析订单",
        worker_result={
            "status": "success",
            "final_answer": "订单分析完成",
            "artifacts": ["/tmp/orders.csv"],
            "error": None,
        },
        status="success",
        last_run_id_executed=0,
    )

    context = ContextFactory.get_context(user_id="u", session_id="s", run_id=1, sub_id=0)
    context.register_query("继续分析", [])

    class _Runtime:
        instructions = ""
        flex_planner_user_sync_pending = True
        env = SimpleNamespace(environment_description="")

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

        def list_builtin_skills(self):
            return []

        def list_user_skills(self):
            return []

        def clear_flex_planner_user_sync_pending(self) -> None:
            self.flex_planner_user_sync_pending = False

    state = {
        "user_id": "u",
        "session_id": "s",
        "run_id": 1,
        "sub_id": 0,
        "user_query": "继续分析",
        "messages": [],
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

    system_content = str(messages[0].content)
    assert "# Subagent Running History" in system_content
    assert '"sub_id": 123456' in system_content
    assert "订单分析完成" in system_content
    assert "last_answer" in system_content
    assert "Subagent Running History" not in str(messages[1].content)


def test_prepare_flex_planner_prompt_skips_worker_metadata_when_swarm_disabled(monkeypatch, tmp_path):
    from dataagent.core.flex.utils.planner_prompt_builder import prepare_flex_planner_prompt

    from dataagent.core.context.context_trajectory import ContextFactory

    ContextFactory.clear_context()
    monkeypatch.setattr(planner_prompt_builder.llm_manager, "get_default_llm", lambda: None)

    context = ContextFactory.get_context(user_id="u", session_id="s", run_id=1, sub_id=0)
    context.register_query("继续分析", [])

    class _Runtime:
        instructions = ""
        flex_planner_user_sync_pending = True
        env = SimpleNamespace(environment_description="")

        def __init__(self) -> None:
            self._cache = {}

        def get_cache(self, key, default=None):
            return self._cache.get(key, default)

        def set_cache(self, key, value):
            self._cache[key] = value

        def get_all_config(self) -> dict:
            return {"SWARM": {"enable": False}}

        def get_runtime_env_prompt(self) -> str:
            return ""

        def list_builtin_skills(self):
            return []

        def list_user_skills(self):
            return []

        def clear_flex_planner_user_sync_pending(self) -> None:
            self.flex_planner_user_sync_pending = False

    state = {
        "user_id": "u",
        "session_id": "s",
        "run_id": 1,
        "sub_id": 0,
        "user_query": "继续分析",
        "messages": [],
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

    assert "# Subagent Running History" not in str(messages[0].content)
