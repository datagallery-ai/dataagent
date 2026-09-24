"""Native graph assembly shared by SDK and hosts: `Runtime` in, Deep Agents graph out.

The bridge between bootstrap's Runtime and extension compilation. Combines compiled
extensions with native host policies and a session-scoped filesystem backend.
No custom Agent runner.
"""

from collections.abc import Sequence

from deepagents import create_deep_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
)
from langchain_core.tools import BaseTool, ToolException
from langchain_openai import ChatOpenAI

from dataagent.bootstrap import Runtime
from dataagent.bootstrap.paths import SessionPaths, WorkspaceInput
from dataagent.extensions import compile_extensions, select_plugins
from dataagent.extensions.filesystem_backend import build_filesystem_backend
from dataagent.extensions.skills import VersionedSkillsMiddleware
from dataagent.extensions.tracing import LocalLangChainTracer
from dataagent.prompts import render_agent_prompt, render_filesystem_prompt, render_subagent_prompt
from dataagent.settings import Limits, Settings


def build_model(settings: Settings):
    config = settings.models[settings.dataagent.model.default]
    return ChatOpenAI(
        model=config.name,
        base_url=config.base_url,
        api_key=config.api_key,
        use_responses_api=False,
        max_retries=config.max_retries,
        timeout=settings.dataagent.limits.timeout_seconds,
    )


def _correctable_error(error, request):
    if isinstance(error, ToolException):
        # Only explicitly classified tool-domain errors are safe to return to the model.
        return "Invalid tool input. Check the tool's schema and supply valid values."
    return None


def build_native_middleware(
    limits: Limits, *, root: bool, compiled_hooks: Sequence[AgentMiddleware] = (),
) -> list[AgentMiddleware]:
    """Accept compiled adapters, not Python handlers; preserve native execution order."""
    return [
        *([TodoListMiddleware()] if root else []),
        ModelCallLimitMiddleware(run_limit=limits.model_calls_per_agent, exit_behavior="error"),
        ToolCallLimitMiddleware(run_limit=limits.tool_calls_per_agent, exit_behavior="error"),
        *compiled_hooks,
        ToolErrorMiddleware(on_error=_correctable_error),
    ]


def load_extensions(runtime: Runtime, *, mcp_tools: Sequence[BaseTool] | None = None) -> dict:
    """Compile Python extensions with already discovered native MCP tools."""
    settings, extensions = runtime.settings, runtime.extensions
    if extensions.mcp_servers and mcp_tools is None:
        raise ValueError("MCP is configured: await load_mcp_tools(runtime), then pass mcp_tools to build_agent")
    if tuple(binding.spec for binding in extensions.hooks) != settings.dataagent.hooks:
        raise ValueError(
            "Independent Hook bindings do not match settings.dataagent.hooks; "
            "use prepare_runtime() or update both settings and extensions.hooks"
        )
    return compile_extensions(
        select_plugins(settings.plugins, extensions.plugin_roots),
        skill_sources=extensions.skill_sources,
        hook_entries=tuple((binding.base_dir, binding.spec) for binding in extensions.hooks),
        mcp_tools=mcp_tools or (),
    )


def build_agent(
    runtime: Runtime, *, checkpointer=None, model=None, session: SessionPaths | None = None,
    input_workspaces: tuple[WorkspaceInput, ...] | None = None,
    mcp_tools: Sequence[BaseTool] | None = None,
    extension_revision: str | None = None,
):
    settings = runtime.settings
    workspaces = runtime.paths.workspaces if input_workspaces is None else input_workspaces
    session = session or runtime.paths.for_session("sdk")
    kwargs = load_extensions(runtime, mcp_tools=mcp_tools)
    backend = build_filesystem_backend(runtime, session)
    filesystem_prompt = render_filesystem_prompt(outputs=session.outputs, workspaces=workspaces)
    limits = settings.dataagent.limits
    for child in kwargs["subagents"]:
        child["system_prompt"] = render_subagent_prompt(
            filesystem_instructions=filesystem_prompt,
            subagent_instructions=child.get("system_prompt"),
        )
        child["middleware"] = [
            *build_native_middleware(limits, root=False, compiled_hooks=child["middleware"]),
        ]
        if extension_revision is not None:
            child["middleware"].append(VersionedSkillsMiddleware(
                backend=backend, sources=child.get("skills", []), revision=extension_revision,
            ))
    kwargs.update(
        model=model if model is not None else build_model(settings),
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
        kwargs["middleware"].append(VersionedSkillsMiddleware(
            backend=backend, sources=kwargs["skills"], revision=extension_revision,
        ))
    return create_deep_agent(**kwargs).with_config(
        callbacks=[LocalLangChainTracer(
            session.traces, runtime.redaction_secrets,
            agent_names=tuple(child["name"] for child in kwargs["subagents"]),
        )],
    )
