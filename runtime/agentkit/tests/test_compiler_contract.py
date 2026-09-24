"""Native translation contracts: scope, order, validation and per-compilation isolation."""

import json
from types import SimpleNamespace

import pytest
from conftest import ScriptedModel, call
from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, HumanMessage

from dataagent.declarations import HookSpec, PluginSpec
from dataagent.extensions import compile_extensions, select_plugins

EXTENSIONS = '''from langchain_core.tools import tool

trace = []

@tool
def increment(value: int) -> int:
    """Increment an integer."""
    return value + 1

async def agent_hook(state, runtime, *, params):
    trace.append(("hook", params["label"], params["event"], params["agent"], None))

async def before_tool(request, *, params):
    trace.append(("hook", params["label"], params["event"], params["agent"],
                  request.tool_call["name"]))

async def after_tool(request, result, *, params):
    trace.append(("hook", params["label"], params["event"], params["agent"],
                  request.tool_call["name"]))
'''


def write_skill(source, name):
    path = source / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nname: {name}\ndescription: Contract skill\n---\nUse the tool.\n")


def hook(event, label, agent="dataagent-v2"):
    handler = event if event in {"before_tool", "after_tool"} else "agent_hook"
    return {"event": event, "entrypoint": f"extensions.py:{handler}",
            "params": {"label": label, "event": event, "agent": agent}}


@pytest.fixture
def contract(tmp_path):
    root = tmp_path / "contract"
    root.mkdir()
    (root / "extensions.py").write_text(EXTENSIONS)
    (root / "root.md").write_text("Root plugin prompt.\n")
    write_skill(root / "skills", "shared")
    write_skill(root / "child-skills", "shared")
    write_skill(tmp_path / "loose", "personal")
    children = []
    for name in ("first", "second"):
        (root / f"{name}.md").write_text(f"Prompt for {name}.\n")
        (root / f"{name}.json").write_text(json.dumps({
            "name": name, "description": f"Delegate {name}", "system_prompt": f"{name}.md",
            "tools": ["increment"], "skills": ["child-skills"],
            "hooks": [hook(event, "child", agent=name) for event in (
                "before_agent", "before_model", "after_model", "before_tool", "after_tool", "after_agent",
            )],
        }))
        children.append(f"{name}.json")
    spec = PluginSpec.model_validate({
        "id": "contract", "system_prompt": "root.md", "skills": ["skills"],
        "tools": {"increment": {"entrypoint": "extensions.py:increment"}},
        "subagents": children,
        "hooks": [hook("before_agent", "plugin"), hook("after_agent", "plugin")],
    })
    return SimpleNamespace(
        root=root, selected=[(root, spec)],
        skill_sources=((str(tmp_path / "loose"), "User"),),
        hook_entries=((root, HookSpec.model_validate(hook("before_agent", "product"))),),
    )


def compile_contract(contract, *, hook_entries=None):
    kwargs = compile_extensions(
        contract.selected, skill_sources=contract.skill_sources,
        hook_entries=contract.hook_entries if hook_entries is None else hook_entries,
    )
    return kwargs, kwargs["tools"][0].func.__globals__


def test_native_contract_order_and_repeat_isolation(contract):
    kwargs, namespace = compile_contract(contract)
    assert set(kwargs) == {"name", "system_prompt", "tools", "skills", "subagents", "middleware"}
    assert kwargs["name"] == "dataagent-v2"
    assert kwargs["system_prompt"] == "Root plugin prompt.\n"
    tool = kwargs["tools"][0]
    assert tool.name == "contract__increment" and tool.description == "Increment an integer."
    assert tool.get_input_schema().model_json_schema() == {
        "description": "Increment an integer.", "title": "increment", "type": "object",
        "properties": {"value": {"title": "Value", "type": "integer"}}, "required": ["value"],
    }
    assert tool.invoke({"value": 4}) == 5
    assert namespace["increment"].name == "increment"
    assert kwargs["skills"] == [(str(contract.root / "skills"), "contract"),
                                (str(contract.root.parent / "loose"), "User")]
    assert [child["name"] for child in kwargs["subagents"]] == ["first", "second"]
    for child in kwargs["subagents"]:
        assert child["description"] == f"Delegate {child['name']}"
        assert child["system_prompt"] == f"Prompt for {child['name']}.\n"
        assert child["tools"] == kwargs["tools"]
        assert child["skills"] == [(str(contract.root / "child-skills"), "contract")]
        assert [item.name for item in child["middleware"]] == [
            *[f"ConfiguredHook__{child['name']}__{event}__{index}" for index, event in enumerate((
                "before_agent", "before_model", "after_model", "before_tool", "after_tool", "after_agent",
            ))],
        ]
    assert [item.name for item in kwargs["middleware"]] == [
        "ConfiguredHook__dataagent-v2__before_agent__0",
        "ConfiguredHook__dataagent-v2__after_agent__1",
        "ConfiguredHook__dataagent-v2__before_agent__2",
    ]
    assert namespace["trace"] == []
    again, other_namespace = compile_contract(contract)
    assert namespace is not other_namespace
    assert kwargs["tools"][0] is not again["tools"][0]
    assert not {id(item) for agent in [kwargs, *kwargs["subagents"]] for item in agent["middleware"]} & {
        id(item) for agent in [again, *again["subagents"]] for item in agent["middleware"]
    }


async def test_native_graph_child_hooks_scope_and_order(contract):
    model = ScriptedModel(responses=[
        call("task", {"subagent_type": "first", "description": "Increment 4"}),
        call("contract__increment", {"value": 4}), AIMessage(content="5"), AIMessage(content="Done"),
    ])
    kwargs, namespace = compile_contract(contract)
    result = await create_deep_agent(model=model, backend=StateBackend(), **kwargs).ainvoke(
        {"messages": [HumanMessage(content="Delegate")]}, {"configurable": {"thread_id": "contract"}},
    )
    assert result["messages"][-1].content == "Done"
    assert [entry for entry in namespace["trace"] if entry[0] == "hook"] == [
        ("hook", "plugin", "before_agent", "dataagent-v2", None),
        ("hook", "product", "before_agent", "dataagent-v2", None),
        ("hook", "child", "before_agent", "first", None),
        ("hook", "child", "before_model", "first", None),
        ("hook", "child", "after_model", "first", None),
        ("hook", "child", "before_tool", "first", "contract__increment"),
        ("hook", "child", "after_tool", "first", "contract__increment"),
        ("hook", "child", "before_model", "first", None),
        ("hook", "child", "after_model", "first", None),
        ("hook", "child", "after_agent", "first", None),
        ("hook", "plugin", "after_agent", "dataagent-v2", None),
    ]


def test_subagent_omitted_tools_vs_explicit_empty(contract):
    for name, omit in [("first", True), ("second", False)]:
        path = contract.root / f"{name}.json"
        data = json.loads(path.read_text())
        data.pop("tools") if omit else data.update(tools=[])
        path.write_text(json.dumps(data))
    kwargs, _ = compile_contract(contract)
    first, second = kwargs["subagents"]
    assert "tools" not in first
    assert second["tools"] == []


def test_no_hooks_means_no_hook_adapter(contract):
    for path in contract.root.glob("*.json"):
        child = json.loads(path.read_text())
        child.pop("hooks")
        path.write_text(json.dumps(child))
    contract.selected = [(root, spec.model_copy(update={"hooks": []})) for root, spec in contract.selected]
    kwargs, _ = compile_contract(contract, hook_entries=())
    assert all(agent["middleware"] == [] for agent in [kwargs, *kwargs["subagents"]])


async def test_standalone_resources_need_no_plugin_or_bindings(contract):
    kwargs = compile_extensions(
        [], skill_sources=contract.skill_sources, hook_entries=contract.hook_entries,
    )
    assert kwargs["skills"] == list(contract.skill_sources)
    assert kwargs["system_prompt"] == ""
    assert kwargs["tools"] == kwargs["subagents"] == []
    compiled_hook, = kwargs["middleware"]
    assert compiled_hook.name == "ConfiguredHook__dataagent-v2__before_agent__0"
    await compiled_hook.abefore_agent({}, None)


def test_explicit_compiler_inputs_are_not_mutated(contract):
    selected = list(contract.selected)
    skills = list(contract.skill_sources)
    hooks = list(contract.hook_entries)
    plugin_before = selected[0][1].model_dump()
    hook_before = hooks[0][1].model_dump()
    for _ in range(2):
        compile_extensions(selected, skill_sources=skills, hook_entries=hooks)
        assert selected == contract.selected
        assert skills == list(contract.skill_sources)
        assert hooks == list(contract.hook_entries)
        assert selected[0][1].model_dump() == plugin_before
        assert hooks[0][1].model_dump() == hook_before


def test_plugin_and_standalone_skills_share_duplicate_validation(contract):
    write_skill(contract.root.parent / "loose", "shared")
    with pytest.raises(ValueError, match="Duplicate Skill name: shared"):
        compile_contract(contract)


def test_external_common_contract(runtime):
    assert runtime.report.plugin_origins == (("common", ("user",)),)
    assert not (runtime.paths.builtin_plugins / "common").exists()
    selected = select_plugins(runtime.settings.plugins, runtime.extensions.plugin_roots)
    kwargs = compile_extensions(selected)
    root = selected[0][0]
    assert root == runtime.paths.home / "plugins/common"
    assert kwargs["system_prompt"] == (root / "prompts/agent.md").read_text()
    general, = kwargs["subagents"]
    assert general["name"] == "general-purpose"
    assert general["system_prompt"] == (root / "prompts/general-purpose.md").read_text()
    assert "tools" not in general  # Inherit tools through the native Deep Agents interface.
    assert kwargs["tools"][0].name == "common__summarize_numbers"
    assert kwargs["tools"][0].invoke({"numbers": [2, 4, 6]}) == {
        "count": 3, "sum": 12, "mean": 4, "min": 2, "max": 6,
    }
    with pytest.raises(ValueError, match="At most 10000"):
        kwargs["tools"][0].invoke({"numbers": [1] * 10001})
    assert general["skills"] == kwargs["skills"] == [(str(root / "skills"), "common")]
    assert len(kwargs["middleware"]) == len(general["middleware"]) == 6


@pytest.mark.parametrize("declared", [None, [], ["child-skills"]])
def test_subagent_skill_inheritance_is_declarative(contract, declared):
    path = contract.root / "first.json"
    child = json.loads(path.read_text())
    if declared is None:
        del child["skills"]
        contract.skill_sources *= 2  # Root and inherited sources use the same deduplication.
    else:
        child["skills"] = declared
    path.write_text(json.dumps(child))
    kwargs, _ = compile_contract(contract)
    compiled = kwargs["subagents"][0]
    expected = kwargs["skills"] if declared is None else [
        (str(contract.root / item), "contract") for item in declared
    ]
    assert compiled["skills"] == expected
    assert compiled["skills"] is not kwargs["skills"]


def test_general_purpose_is_not_reserved_but_must_be_unique(contract):
    for name in ("first", "second"):
        path = contract.root / f"{name}.json"
        child = json.loads(path.read_text())
        child["name"] = "general-purpose"
        path.write_text(json.dumps(child))
    with pytest.raises(ValueError, match="Duplicate or reserved SubAgent name: general-purpose"):
        compile_contract(contract)


def test_disabled_common_contributes_no_compiled_capabilities():
    kwargs = compile_extensions([])
    assert kwargs["subagents"] == kwargs["tools"] == kwargs["skills"] == kwargs["middleware"] == []
