"""Ordinary Python hooks retain native objects, reducers and invocation semantics."""

import asyncio
import operator
from functools import partial
from types import SimpleNamespace
from typing import Annotated

import pytest
from conftest import ScriptedModel, call
from deepagents import create_deep_agent
from deepagents.graph import DeepAgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.errors import NodeCancelledError
from langgraph.runtime import Runtime
from langgraph.types import Command

from dataagent.agent import build_native_middleware
from dataagent.declarations import HookSpec
from dataagent.extensions.hooks import compile_hook
from dataagent.settings import Limits


def compile_function(event, handler, *, params=None, name="ConfiguredHook", matcher=None):
    spec = HookSpec(event=event, entrypoint="hooks.py:handle", params=params or {}, matcher=matcher)
    return compile_hook(spec, handler, name=name)


@pytest.mark.parametrize("event", ["before_tool", "after_tool"])
@pytest.mark.parametrize("matcher,expected", [(None, 1), ("tool", 1), ("other", 0), ("t*", 0)])
async def test_tool_matcher_filters_hooks_without_filtering_execution(event, matcher, expected):
    seen, executed = [], []
    request = SimpleNamespace(tool_call={"name": "tool"})
    result = object()

    def before(request, *, params):
        seen.append(request)

    def after(request, result, *, params):
        seen.append(request)

    def execute(actual):
        executed.append(actual)
        return result

    async def aexecute(actual):
        return execute(actual)

    adapter = compile_function(event, before if event == "before_tool" else after, matcher=matcher)
    assert adapter.wrap_tool_call(request, execute) is result
    assert await adapter.awrap_tool_call(request, aexecute) is result
    assert len(seen) == expected * 2
    assert executed == [request, request]


@pytest.mark.parametrize("event", ["before_agent", "after_agent", "before_model", "after_model"])
def test_state_hook_preserves_native_identity_and_copies_config(event):
    state = {"messages": [HumanMessage(content="question")], "custom": object()}
    runtime = Runtime(context=object())
    update = {"custom": state["custom"]}
    params = {"nested": ["original"]}
    seen = []

    def handler(actual_state, actual_runtime, *, params):
        assert actual_state is state and actual_runtime is runtime
        seen.append(params["nested"][0])
        params["nested"][0] = "mutated"
        return update

    compiled_hook = compile_function(event, handler, params=params)
    assert isinstance(compiled_hook, AgentMiddleware)
    assert compiled_hook.name == "ConfiguredHook"
    assert getattr(compiled_hook, event)(state, runtime) is update
    assert getattr(compiled_hook, event)(state, runtime) is update
    assert seen == ["original", "original"]
    assert params == {"nested": ["original"]}


@pytest.mark.parametrize("event", ["before_agent", "after_agent", "before_model", "after_model"])
async def test_async_state_hook_preserves_native_command(event):
    state, runtime = {}, Runtime(context=object())
    command = Command(update={"custom": object()})

    async def handler(actual_state, actual_runtime, *, params):
        assert actual_state is state and actual_runtime is runtime
        return command

    compiled_hook = compile_function(event, handler)
    assert await getattr(compiled_hook, f"a{event}")(state, runtime) is command


@pytest.mark.parametrize("handler", [
    None,
    lambda event, params: None,
    lambda state, runtime, params: None,
    lambda state, runtime, **kwargs: None,
    lambda state, runtime, *, params, extra: None,
    partial(lambda state, runtime, *, params: None),
])
def test_invalid_state_signatures_fail_at_compilation(handler):
    with pytest.raises(TypeError, match=r"plain def/async def.*\*, params"):
        compile_function("before_agent", handler)


def test_generator_functions_and_callable_instances_are_not_plain_hooks():
    def generator(state, runtime, *, params):
        yield {}

    async def async_generator(state, runtime, *, params):
        yield {}

    class CallableObject:
        def __call__(self, state, runtime, *, params):
            return None

    for handler in [generator, async_generator, CallableObject()]:
        with pytest.raises(TypeError, match="plain def/async def"):
            compile_function("before_model", handler)


@pytest.mark.parametrize("event,handler", [
    ("before_tool", lambda event, params: None),
    ("before_tool", lambda request, result, *, params: None),
    ("after_tool", lambda request, *, params: None),
    ("after_tool", lambda request, result, params: None),
])
def test_invalid_tool_signatures_fail_at_compilation(event, handler):
    with pytest.raises(TypeError, match="migrate old handle"):
        compile_function(event, handler)


@pytest.mark.parametrize("result", [
    {"action": "continue"}, {"action": "deny", "reason": "blocked"}, "continue", True,
])
def test_state_hooks_reject_old_protocol_and_invalid_updates(result):
    def handler(state, runtime, *, params):
        return result

    with pytest.raises(TypeError, match="Hook"):
        compile_function("before_agent", handler).before_agent({}, Runtime())


@pytest.mark.parametrize("is_async", [False, True])
async def test_native_state_schema_context_and_reducer_are_preserved(is_async):
    class CustomState(DeepAgentState):
        entries: Annotated[list[str], operator.add]

    context = SimpleNamespace(user="alice")

    def update(state, runtime, *, params):
        assert runtime.context is context
        assert state["entries"] == ["initial"]
        return {"entries": ["before"], "messages": [HumanMessage(content="context", id="context")]}

    async def aupdate(state, runtime, *, params):
        return update(state, runtime, params=params)

    def second_update(state, runtime, *, params):
        assert state["entries"] == ["initial", "before"]
        return {"entries": ["second"], "messages": [
            HumanMessage(content="updated context", id="context"),
        ]}

    def finish(state, runtime, *, params):
        assert state["entries"] == ["initial", "before", "second"]
        return Command(update={"entries": ["after"]})

    model = ScriptedModel(responses=[AIMessage(content="done")])
    graph = create_deep_agent(
        model=model, state_schema=CustomState, context_schema=SimpleNamespace,
        middleware=[
            compile_function("before_agent", aupdate if is_async else update, name="Before"),
            compile_function("before_agent", second_update, name="SecondBefore"),
            compile_function("after_agent", finish, name="After"),
        ],
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="hello", id="original")], "entries": ["initial"]},
        context=context,
    )
    assert result["entries"] == ["initial", "before", "second", "after"]
    assert [message.content for message in result["messages"]] == ["hello", "updated context", "done"]


def test_sync_native_graph_invocation_accepts_sync_state_hook():
    seen = []

    def handler(state, runtime, *, params):
        seen.append(state["messages"][0].content)

    graph = create_deep_agent(
        model=ScriptedModel(responses=[AIMessage(content="done")]),
        middleware=[compile_function("before_agent", handler)],
    )
    assert graph.invoke({"messages": [("user", "hello")]})["messages"][-1].content == "done"
    assert seen == ["hello"]


def test_async_state_hook_is_not_run_in_an_implicit_event_loop():
    seen = []

    async def handler(state, runtime, *, params):
        seen.append(True)

    graph = create_deep_agent(
        model=ScriptedModel(responses=[AIMessage(content="done")]),
        middleware=[compile_function("before_agent", handler)],
    )
    with pytest.raises(TypeError, match="sync"):
        graph.invoke({"messages": [("user", "hello")]})
    assert seen == []


@pytest.mark.parametrize("async_hook", [False, True])
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_state_hook_failures_follow_native_graph_semantics(async_hook, error_type):
    finished = []
    error = error_type("original failure")

    def handler(state, runtime, *, params):
        raise error

    async def async_handler(state, runtime, *, params):
        raise error

    def after(state, runtime, *, params):
        finished.append(True)

    model = ScriptedModel(responses=[AIMessage(content="not reached")])
    graph = create_deep_agent(model=model, middleware=[
        compile_function("before_model", async_handler if async_hook else handler, name="Before"),
        compile_function("after_agent", after, name="After"),
    ])
    expected = NodeCancelledError if error_type is asyncio.CancelledError else error_type
    with pytest.raises(expected) as raised:
        await graph.ainvoke({"messages": [("user", "hello")]})
    # LangGraph itself distinguishes user-raised cancellation from actual task cancellation.
    assert (raised.value.__cause__ if expected is NodeCancelledError else raised.value) is error
    assert model.cursor == 0 and not finished


async def test_actual_cancellation_of_async_hook_propagates_and_skips_after():
    entered = asyncio.Event()
    finished = []

    async def wait(state, runtime, *, params):
        entered.set()
        await asyncio.Event().wait()

    def after(state, runtime, *, params):
        finished.append(True)

    graph = create_deep_agent(
        model=ScriptedModel(responses=[AIMessage(content="not reached")]),
        middleware=[compile_function("before_model", wait, name="Before"),
                    compile_function("after_agent", after, name="After")],
    )
    task = asyncio.create_task(graph.ainvoke({"messages": [("user", "hello")]}))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not finished


@pytest.mark.parametrize("event", ["before_tool", "after_tool"])
@pytest.mark.parametrize("async_hook", [False, True])
@pytest.mark.parametrize("result_type", ["success", "error", "command"])
async def test_tool_hooks_receive_actual_request_and_result(event, async_hook, result_type):
    request = SimpleNamespace(tool_call={"name": "tool"}, runtime=object())
    message = ToolMessage(content="output", tool_call_id="call", status=(
        "error" if result_type == "error" else "success"
    ))
    result = Command(update={"messages": [message]}) if result_type == "command" else message
    seen = []

    def before(actual_request, *, params):
        assert actual_request is request
        seen.append(params["nested"][0])
        params["nested"][0] = "changed"

    def after(actual_request, actual_result, *, params):
        assert actual_request is request and actual_result is result
        seen.append(params["nested"][0])
        params["nested"][0] = "changed"

    async def async_before(actual_request, *, params):
        return before(actual_request, params=params)

    async def async_after(actual_request, actual_result, *, params):
        return after(actual_request, actual_result, params=params)

    async def execute(actual_request):
        assert actual_request is request
        return result

    handler = (async_before if async_hook else before) if event == "before_tool" else (
        async_after if async_hook else after
    )
    compiled_hook = compile_function(event, handler, params={"nested": ["original"]})
    assert compiled_hook.name == "ConfiguredHook"
    assert await compiled_hook.awrap_tool_call(request, execute) is result
    assert await compiled_hook.awrap_tool_call(request, execute) is result
    assert seen == ["original", "original"]
    if not async_hook:
        assert compiled_hook.wrap_tool_call(request, lambda req: result) is result


@pytest.mark.parametrize("event", ["before_tool", "after_tool"])
def test_async_tool_hook_does_not_run_tool_in_sync_path(event):
    async def before(request, *, params):
        return None

    async def after(request, result, *, params):
        return None

    executed = []
    compiled_hook = compile_function(event, before if event == "before_tool" else after)
    with pytest.raises(TypeError, match="ainvoke/astream"):
        compiled_hook.wrap_tool_call(object(), lambda request: executed.append(True))
    assert executed == []


@pytest.mark.parametrize("error", [RuntimeError("original"), asyncio.CancelledError()])
async def test_tool_exception_and_cancel_skip_after_hook(error):
    seen = []

    def after(request, result, *, params):
        seen.append(result)

    async def execute(request):
        raise error

    compiled_hook = compile_function("after_tool", after)
    with pytest.raises(type(error)) as raised:
        await compiled_hook.awrap_tool_call(object(), execute)
    assert raised.value is error
    assert seen == []


@pytest.mark.parametrize("event", ["before_tool", "after_tool"])
async def test_tool_hook_failure_is_not_wrapped_or_swallowed(event):
    error = ValueError("policy or observer failure")
    executed = []

    def before(request, *, params):
        raise error

    def after(request, result, *, params):
        raise error

    async def execute(request):
        executed.append(True)
        return object()

    compiled_hook = compile_function(event, before if event == "before_tool" else after)
    with pytest.raises(ValueError) as raised:
        await compiled_hook.awrap_tool_call(object(), execute)
    assert raised.value is error
    assert executed == ([] if event == "before_tool" else [True])


async def test_tool_hook_must_not_return_old_decision_or_replace_result():
    def before(request, *, params):
        return {"action": "continue"}

    def after(request, result, *, params):
        return ToolMessage(content="replacement", tool_call_id="call")

    async def execute(request):
        return object()

    for event, function in [("before_tool", before), ("after_tool", after)]:
        with pytest.raises(TypeError, match="Tool Hook must return None"):
            await compile_function(event, function).awrap_tool_call(object(), execute)


async def test_native_multiple_declaration_order_before_forward_after_reverse():
    seen = []

    def state_hook(state, runtime, *, params):
        seen.append(params["label"])

    def before(request, *, params):
        seen.append(params["label"])

    def after(request, result, *, params):
        seen.append(params["label"])

    @tool
    def echo(value: str) -> str:
        """Return the provided value."""
        return value

    events = ["before_agent", "before_model", "before_tool", "after_tool", "after_model", "after_agent"]
    compiled_hooks = [
        compile_function(
            event, before if event == "before_tool" else after if event == "after_tool" else state_hook,
            params={"label": f"{event}-{index}"}, name=f"Configured_{event}_{index}",
        )
        for event in events for index in (1, 2)
    ]
    graph = create_deep_agent(
        model=ScriptedModel(responses=[call("echo", {"value": "hi"}), AIMessage(content="done")]),
        tools=[echo], middleware=compiled_hooks,
    )
    await graph.ainvoke({"messages": [("user", "echo hi")]})
    expected_events = ["before_agent", "before_model", "after_model", "before_tool", "after_tool",
                       "before_model", "after_model", "after_agent"]
    assert seen == [f"{event}-{index}" for event in expected_events
                    for index in ((1, 2) if event.startswith("before") else (2, 1))]


@pytest.mark.parametrize("root", [False, True])
def test_native_assembly_preserves_compiled_hook_identity_order_and_independent_policies(root):
    def handler(state, runtime, *, params):
        return None

    compiled_hooks = tuple(compile_function("before_model", handler, name=f"Hook_{index}")
                           for index in (1, 2))
    native_middleware = build_native_middleware(Limits(), root=root, compiled_hooks=compiled_hooks)
    assert [item.name for item in native_middleware] == [
        *(["TodoListMiddleware"] if root else []),
        "ModelCallLimitMiddleware", "ToolCallLimitMiddleware", "Hook_1", "Hook_2", "ToolErrorMiddleware",
    ]
    offset = 3 if root else 2
    assert all(native_middleware[offset + index] is item for index, item in enumerate(compiled_hooks))
    again = build_native_middleware(Limits(), root=root, compiled_hooks=compiled_hooks)
    assert all(again[index] is not native_middleware[index] for index in (*range(offset), -1))
    assert len(compiled_hooks) == 2  # Assembly does not append policies into the caller's list.
