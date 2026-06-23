# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""YAML-to-OpenJiuWen local tool adapter tests."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from dataagent.core.deep_agent.adapter import DeepAgentAdapter
from dataagent.core.deep_agent.builders.tools.local import build_local_tools
from dataagent.core.deep_agent.prompt_builder import build_system_prompt
from dataagent.core.deep_agent.spec import DeepAgentBuildSpec, LocalToolSpec

FIXTURE_MODULE = "tests.ut.deep_agent.local_tool_fixtures"


def _spec(function: str, **overrides: object) -> LocalToolSpec:
    values: dict[str, Any] = {
        "path": "TOOLS.local_functions[0]",
        "module": FIXTURE_MODULE,
        "function": function,
        "name": function,
    }
    values.update(overrides)
    return LocalToolSpec(**values)


def test_normalizes_local_tool_yaml() -> None:
    spec = DeepAgentBuildSpec.from_config(
        {
            "TOOLS": {
                "local_functions": [
                    {
                        "module": FIXTURE_MODULE,
                        "function": "add_numbers",
                        "name": "sum_numbers",
                        "description": "Add configured numbers.",
                        "category": "math",
                    }
                ]
            }
        }
    )

    assert spec.local_tools == (
        LocalToolSpec(
            path="TOOLS.local_functions[0]",
            module=FIXTURE_MODULE,
            function="add_numbers",
            name="sum_numbers",
            description="Add configured numbers.",
            category="math",
        ),
    )


def test_normalizes_bash_allowlist_three_state_config() -> None:
    assert DeepAgentBuildSpec.from_config({}).bash_allowlist is None
    assert DeepAgentBuildSpec.from_config({"BASH_TOOL_WHITELIST": None}).bash_allowlist is None
    assert DeepAgentBuildSpec.from_config({"BASH_TOOL_WHITELIST": []}).bash_allowlist == ()
    assert DeepAgentBuildSpec.from_config(
        {"BASH_TOOL_WHITELIST": ["ls", " python ", "ls"]}
    ).bash_allowlist == ("ls", "python")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("ls", "must be a list"),
        ([1], r"\[0\].*string"),
        ([" "], r"\[0\].*must not be empty"),
    ],
)
def test_rejects_invalid_bash_allowlist(value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        DeepAgentBuildSpec.from_config({"BASH_TOOL_WHITELIST": value})


def test_bash_allowlist_prompt_matches_runtime_state() -> None:
    assert "Bash 工具已禁用" in build_system_prompt({"BASH_TOOL_WHITELIST": []})
    restricted = build_system_prompt({"BASH_TOOL_WHITELIST": ["ls", "cat"]})
    assert "你只能执行以下命令" in restricted
    assert "`ls`" in restricted
    assert "Bash 命令限制" not in build_system_prompt({})


def test_sub_agent_tool_is_deferred_to_subagent_adapter() -> None:
    spec = DeepAgentBuildSpec.from_config(
        {
            "TOOLS": {
                "local_functions": [
                    {
                        "module": "dataagent.actions.tools.local_tool.tools",
                        "function": "sub_agent_tool",
                    }
                ]
            }
        }
    )

    assert spec.local_tools == ()
    assert "Subagent adapter" in spec.diagnostics[0]


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"TOOLS": {"local_functions": "bad"}}, "must be a list"),
        ({"TOOLS": {"local_functions": [{}]}}, ".module is required"),
        (
            {
                "TOOLS": {
                    "local_functions": [
                        {"module": FIXTURE_MODULE, "function": "add_numbers", "name": "duplicate"},
                        {"module": FIXTURE_MODULE, "function": "async_echo", "name": "duplicate"},
                    ]
                }
            },
            "duplicates local tool",
        ),
    ],
)
def test_rejects_invalid_local_tool_yaml(config: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        DeepAgentBuildSpec.from_config(config)


@pytest.mark.asyncio
async def test_builds_sync_tool_with_schema_and_runs_in_worker_thread() -> None:
    built = build_local_tools(
        [
            _spec(
                "add_numbers",
                name="sum_numbers",
                description="Add configured numbers.",
                category="math",
            ),
            _spec("current_thread_id"),
        ]
    )
    sum_tool, thread_tool = built

    assert sum_tool.card.name == "sum_numbers"
    assert sum_tool.card.description == "Add configured numbers."
    assert sum_tool.card.input_params["required"] == ["a"]
    assert set(sum_tool.card.input_params["properties"]) == {"a", "b"}
    assert sum_tool.card.properties["dataagent.category"] == "math"
    assert await sum_tool.invoke({"a": 2, "b": 3}) == 5

    main_thread_id = threading.get_ident()
    assert await thread_tool.invoke({}) != main_thread_id


@pytest.mark.asyncio
async def test_builds_async_tool_without_changing_behavior() -> None:
    async_tool = build_local_tools([_spec("async_echo")])[0]

    assert await async_tool.invoke({"message": "hello"}) == "hello"


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (_spec("missing_function"), "was not found"),
        (_spec("NOT_CALLABLE"), "must be a Python function"),
        (_spec("generated_values"), "is a generator"),
        (_spec("needs_tool_context"), "requires _tool_context"),
        (
            LocalToolSpec(
                path="TOOLS.local_functions[0]",
                module="tests.ut.deep_agent.missing_module",
                function="tool",
                name="tool",
            ),
            "failed to import",
        ),
    ],
)
def test_reports_build_errors_with_yaml_path(spec: LocalToolSpec, message: str) -> None:
    with pytest.raises(ValueError, match=message) as exc_info:
        build_local_tools([spec])

    assert "TOOLS.local_functions[0]" in str(exc_info.value)


def test_adapter_combines_harness_and_local_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    harness_tool = object()
    local_tool = object()

    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.build_harness_tools",
        lambda sys_operation, language, **kwargs: [harness_tool],
    )
    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.build_local_tools",
        lambda specs: [local_tool],
    )
    adapter = DeepAgentAdapter(
        {
            "TOOLS": {
                "local_functions": [
                    {
                        "module": FIXTURE_MODULE,
                        "function": "add_numbers",
                    }
                ]
            }
        }
    )

    assert adapter.build_tools(object()) == [harness_tool, local_tool]


def test_adapter_rejects_name_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCard:
        name = "duplicate"

    class FakeTool:
        card = FakeCard()

    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.build_harness_tools",
        lambda sys_operation, language, **kwargs: [FakeTool()],
    )
    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.build_local_tools",
        lambda specs: [FakeTool()],
    )
    adapter = DeepAgentAdapter({"TOOLS": {}})
    with pytest.raises(ValueError, match="conflicts"):
        adapter.build_tools(object())
