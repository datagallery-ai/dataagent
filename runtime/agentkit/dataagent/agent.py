"""Native graph assembly shared by SDK and hosts: `Runtime` in, one graph out.

The bridge between bootstrap's Runtime and extension compilation. Combines compiled
extensions with native host policies and a session-scoped filesystem backend.
No custom Agent runner. ``dataagent.entry`` mounts that compiled subagent instead.
"""

from collections.abc import Callable, Sequence
from pathlib import Path

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, ToolException
from langgraph.graph import END, START, MessagesState, StateGraph

from dataagent.bootstrap import Runtime
from dataagent.bootstrap.paths import SessionPaths, WorkspaceInput
from dataagent.core.graph import _RECURSION_LIMIT, create_deep_agent
from dataagent.core.middleware.filesystem.filesystem import FilesystemMiddleware
from dataagent.extensions import compile_extensions, select_plugins
from dataagent.extensions.filesystem_backend import build_filesystem_backend
from dataagent.extensions.skills import VersionedSkillsMiddleware
from dataagent.extensions.subagents import load_declared_subagents
from dataagent.extensions.tracing import LocalLangChainTracer
from dataagent.models import ModelContext
from dataagent.prompts import render_agent_prompt, render_filesystem_prompt, render_subagent_prompt
from dataagent.semantic_client import SemanticServiceClient
from dataagent.settings import Limits


def _correctable_error(error, request):
    if isinstance(error, ToolException):
        # Only explicitly classified tool-domain errors are safe to return to the model.
        return "Invalid tool input. Check the tool's schema and supply valid values."
    return None


def build_native_middleware(
    limits: Limits,
    *,
    root: bool,
    compiled_hooks: Sequence[AgentMiddleware] = (),
) -> list[AgentMiddleware]:
    """Accept compiled adapters, not Python handlers; preserve native execution order."""
    return [
        *([TodoListMiddleware()] if root else []),
        ModelCallLimitMiddleware(run_limit=limits.model_calls_per_agent, exit_behavior="error"),
        ToolCallLimitMiddleware(run_limit=limits.tool_calls_per_agent, exit_behavior="error"),
        *compiled_hooks,
        ToolErrorMiddleware(on_error=_correctable_error),
    ]


def _check_extension_inputs(runtime, mcp_tools):
    settings, extensions = runtime.settings, runtime.extensions
    if extensions.mcp_servers and mcp_tools is None:
        raise ValueError(
            "MCP is configured: await load_mcp_tools(runtime), then pass mcp_tools to build_agent"
        )
    if tuple(binding.spec for binding in extensions.hooks) != settings.dataagent.hooks:
        raise ValueError(
            "Independent Hook bindings do not match settings.dataagent.hooks; "
            "use prepare_runtime() or update both settings and extensions.hooks"
        )


def check_agent_configuration(runtime, *, semantic_client, mcp_tools=None) -> None:
    """Reject what build_agent would reject, without creating sessions or running factories."""
    if runtime.settings.semantic is not None and semantic_client is None:
        raise ValueError(
            "Semantic service is configured: use create_agent(), or supply a ModelContext "
            "with a host-owned semantic_client to build_agent() and native graph invocations"
        )
    _check_extension_inputs(runtime, mcp_tools)
    if runtime.settings.dataagent.entry:
        _require_declared_entry(runtime, runtime.settings.dataagent.entry)
        return
    for root, spec in select_plugins(runtime.settings.plugins, runtime.extensions.plugin_roots):
        load_declared_subagents(root, spec)


def load_extensions(
    runtime: Runtime,
    *,
    mcp_tools: Sequence[BaseTool] | None = None,
    semantic_client: SemanticServiceClient | None = None,
    model_resolver: Callable[[str], BaseChatModel] | None = None,
    outputs_dir: Path | None = None,
    context: ModelContext | None = None,
) -> dict:
    """Compile Python extensions with already discovered native MCP tools.

    ``semantic_client`` is the host-owned semantic-service client handed to
    compiled-subagent factories; build_agent supplies the context's client.
    ``model_resolver`` maps model aliases to models for compiled subagents;
    build_agent supplies the context's cached resolver.
    ``outputs_dir`` is the session output directory for compiled-subagent
    result files; build_agent supplies the session's ``outputs``.
    """
    settings, extensions = runtime.settings, runtime.extensions
    _check_extension_inputs(runtime, mcp_tools)
    a2a_tools = ()
    if settings.a2a.agents:
        from a2a_server.client import load_a2a_tools

        a2a_tools = load_a2a_tools(runtime)
    return compile_extensions(
        select_plugins(settings.plugins, extensions.plugin_roots),
        skill_sources=extensions.skill_sources,
        hook_entries=tuple((binding.base_dir, binding.spec) for binding in extensions.hooks),
        mcp_tools=mcp_tools or (),
        a2a_tools=a2a_tools,
        plugin_params=dict(settings.plugins.config),
        model_resolver=model_resolver
        if model_resolver is not None
        else ModelContext(runtime).get_model,
        semantic_client=semantic_client,
        outputs_dir=outputs_dir,
        context=context,
        run_hook_entries={
            key: [(binding.base_dir, binding.spec) for binding in bindings]
            for key, bindings in extensions.run_hooks.items()
        },
    )


def _require_declared_entry(runtime, entry):
    """Reject an unknown or standard entry before plugin factories run."""
    compiled, standard = [], []
    selected = select_plugins(runtime.settings.plugins, runtime.extensions.plugin_roots)
    for root, spec in selected:
        for child in load_declared_subagents(root, spec):
            (compiled if child.kind == "compiled" else standard).append(child.name)
    if entry in compiled:
        return
    if entry in standard:
        raise ValueError(
            f"dataagent.entry {entry!r} is a standard subagent; "
            "only compiled subagents can be entries."
        )
    names = ", ".join(compiled) if compiled else "(none)"
    raise ValueError(
        f"dataagent.entry {entry!r} does not match a compiled subagent. "
        f"Available compiled subagents: {names}."
    )


def build_entry_graph(runnable, *, entry, checkpointer, session, context):
    """Mount one compiled-subagent Runnable as the whole graph."""
    graph = StateGraph(MessagesState, context_schema=ModelContext)
    graph.add_node(entry, runnable)
    graph.add_edge(START, entry)
    graph.add_edge(entry, END)
    compiled = graph.compile(checkpointer=checkpointer, name=entry)
    return compiled.with_config(
        recursion_limit=_RECURSION_LIMIT,
        callbacks=[
            LocalLangChainTracer(
                session.traces, context.redaction_secrets, agent_names=(entry,),
            )
        ],
    )


def _build_compiled_entry(runtime, *, checkpointer, session, context, mcp_tools):
    """Resolve ``dataagent.entry`` and mount it with ``build_entry_graph``."""
    entry = runtime.settings.dataagent.entry
    kwargs = load_extensions(
        runtime, mcp_tools=mcp_tools, semantic_client=context.semantic_client,
        model_resolver=context.get_model, outputs_dir=session.outputs,
        context=context,
    )
    try:
        runnable = next(
            child["runnable"] for child in kwargs["subagents"]
            if child.get("name") == entry and "runnable" in child
        )
    except StopIteration:
        raise ValueError(
            f"dataagent.entry {entry!r} did not produce a compiled runnable"
        ) from None
    return build_entry_graph(
        runnable, entry=entry, checkpointer=checkpointer,
        session=session, context=context,
    )


def build_agent(
    runtime: Runtime,
    *,
    checkpointer=None,
    model=None,
    session: SessionPaths | None = None,
    input_workspaces: tuple[WorkspaceInput, ...] | None = None,
    mcp_tools: Sequence[BaseTool] | None = None,
    extension_revision: str | None = None,
    context: ModelContext | None = None,
):
    """Return the native graph; raw callers pass context on invocation or use create_agent."""
    explicit_session = session or (context.session if context is not None else None)
    session = explicit_session or runtime.paths.for_session("sdk")
    context = (
        context if context is not None else ModelContext(runtime, model=model, session=session)
    )
    if context.session != session:
        raise ValueError("Agent context and filesystem must refer to the same session")
    settings = runtime.settings
    check_agent_configuration(runtime, semantic_client=context.semantic_client, mcp_tools=mcp_tools)
    if explicit_session is not None:
        expected = runtime.paths.for_session(session.root.name)
        if session.root != expected.root:
            raise ValueError("session.root must match this profile's Home runtime path")
        if session.traces != expected.traces:
            raise ValueError("session.traces must use the standard Home path")
    # Raw graph callers use the same registration entry as SDK/HTTP. Already-open
    # sessions only validate their saved paths; assembly never writes bindings directly.
    session, workspaces = runtime.paths.open_session(
        session.root.name, create=True,
        output_dir=explicit_session.outputs if explicit_session is not None else None,
        input_workspaces=input_workspaces,
    )
    if context.session != session:
        context = context.for_session(session)
    if settings.dataagent.entry:
        return _build_compiled_entry(
            runtime, checkpointer=checkpointer, session=session, context=context,
            mcp_tools=mcp_tools,
        )
    kwargs = load_extensions(
        runtime,
        mcp_tools=mcp_tools,
        semantic_client=context.semantic_client,
        model_resolver=context.get_model,
        context=context,
        outputs_dir=session.outputs,
    )
    backend = build_filesystem_backend(runtime, session)
    filesystem_prompt = render_filesystem_prompt(outputs=session.outputs, workspaces=workspaces)
    limits = settings.dataagent.limits
    for child in kwargs["subagents"]:
        if "runnable" in child:
            # Compiled subagents arrive as complete runnables; the core applies its own policies.
            continue
        child["system_prompt"] = render_subagent_prompt(
            filesystem_instructions=filesystem_prompt,
            subagent_instructions=child.get("system_prompt"),
        )
        child["middleware"] = [
            *build_native_middleware(limits, root=False, compiled_hooks=child["middleware"]),
        ]
        filesystem_tools = child.pop("filesystem_tools", None)
        if filesystem_tools is not None:
            child["middleware"].append(
                FilesystemMiddleware(backend=backend, tools=filesystem_tools)
            )
        if extension_revision is not None:
            child["middleware"].append(
                VersionedSkillsMiddleware(
                    backend=backend,
                    sources=child.get("skills", []),
                    revision=extension_revision,
                )
            )
    kwargs.update(
        model=context.get_model(),
        context_schema=ModelContext,
        backend=backend,
        checkpointer=checkpointer,
        system_prompt=render_agent_prompt(
            filesystem_instructions=filesystem_prompt,
            extension_instructions=kwargs["system_prompt"],
        ),
        middleware=[
            *build_native_middleware(limits, root=True, compiled_hooks=kwargs["middleware"]),
        ],
    )
    if extension_revision is not None:
        kwargs["middleware"].append(
            VersionedSkillsMiddleware(
                backend=backend,
                sources=kwargs["skills"],
                revision=extension_revision,
            )
        )
    return create_deep_agent(**kwargs).with_config(
        callbacks=[
            LocalLangChainTracer(
                session.traces,
                context.redaction_secrets,
                agent_names=tuple(child["name"] for child in kwargs["subagents"]),
            )
        ],
    )
