"""Compile DataAgent hook configuration into native LangChain middleware."""

import functools
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langgraph.runtime import Runtime
from loguru import logger

from dataagent.core.deepagents.hooks import invoke_hook
from dataagent.core.deepagents.state import DataAgentState
from dataagent.utils.import_utils import import_callable_from_spec

HookCallable = Callable[..., Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None]]

_REMOVED_FLEX_HOOK_NAMES = frozenset(
    {
        "context_reference_rewriter",
        "cross_session_recall",
        "hitl_auto_resolver",
        "human_feedback_guard",
        "intent_understanding",
        "organize_workspace",
        "plan_enforcer",
        "portraiter",
        "post_metadata_tracker",
        "pre_metadata_tracker",
        "pruner",
        "semantic_retrieve_context_loader",
    }
)


@dataclass(frozen=True)
class _ResolvedHook:
    callable: HookCallable
    location: str


class HookMiddleware(AgentMiddleware[DataAgentState, None, Any]):
    """Run configured agent and model hook chains with native LangGraph runtime."""

    state_schema = DataAgentState

    def __init__(
        self,
        *,
        agent_pre: Sequence[_ResolvedHook] = (),
        agent_post: Sequence[_ResolvedHook] = (),
        model_pre: Sequence[_ResolvedHook] = (),
        model_post: Sequence[_ResolvedHook] = (),
    ) -> None:
        self._agent_pre = tuple(agent_pre)
        self._agent_post = tuple(agent_post)
        self._model_pre = tuple(model_pre)
        self._model_post = tuple(model_post)

    def before_agent(self, state: DataAgentState, runtime: Runtime[None]) -> dict[str, Any] | None:
        """Run ``HOOKS.agent.pre`` before one agent invocation."""
        return self._run_sync(self._agent_pre, state, runtime)

    async def abefore_agent(self, state: DataAgentState, runtime: Runtime[None]) -> dict[str, Any] | None:
        """Asynchronously run ``HOOKS.agent.pre`` before one agent invocation."""
        return await self._run_async(self._agent_pre, state, runtime)

    def before_model(self, state: DataAgentState, runtime: Runtime[None]) -> dict[str, Any] | None:
        """Run compatible planner pre-hooks and ``HOOKS.model.pre`` before each model call."""
        return self._run_sync(self._model_pre, state, runtime)

    async def abefore_model(self, state: DataAgentState, runtime: Runtime[None]) -> dict[str, Any] | None:
        """Asynchronously run compatible planner pre-hooks and model pre-hooks."""
        return await self._run_async(self._model_pre, state, runtime)

    def after_model(self, state: DataAgentState, runtime: Runtime[None]) -> dict[str, Any] | None:
        """Run compatible planner post-hooks and ``HOOKS.model.post`` after each model call."""
        return self._run_sync(self._model_post, state, runtime)

    async def aafter_model(self, state: DataAgentState, runtime: Runtime[None]) -> dict[str, Any] | None:
        """Asynchronously run compatible planner post-hooks and model post-hooks."""
        return await self._run_async(self._model_post, state, runtime)

    def after_agent(self, state: DataAgentState, runtime: Runtime[None]) -> dict[str, Any] | None:
        """Run ``HOOKS.agent.post`` after one agent invocation."""
        return self._run_sync(self._agent_post, state, runtime)

    async def aafter_agent(self, state: DataAgentState, runtime: Runtime[None]) -> dict[str, Any] | None:
        """Asynchronously run ``HOOKS.agent.post`` after one agent invocation."""
        return await self._run_async(self._agent_post, state, runtime)

    @staticmethod
    def _run_sync(
        hooks: Sequence[_ResolvedHook],
        state: DataAgentState,
        runtime: Runtime[None],
    ) -> dict[str, Any] | None:
        current = dict(state)
        updates: dict[str, Any] = {}
        for hook in hooks:
            if inspect.iscoroutinefunction(hook.callable):
                raise RuntimeError(f"{hook.location} is async; invoke the agent with ainvoke() or astream().")
            before = dict(current)
            result = invoke_hook(hook.callable, current, runtime)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise RuntimeError(f"{hook.location} returned an awaitable during synchronous execution.")
            HookMiddleware._merge_result(hook, before, current, updates, result)
        return updates or None

    @staticmethod
    async def _run_async(
        hooks: Sequence[_ResolvedHook],
        state: DataAgentState,
        runtime: Runtime[None],
    ) -> dict[str, Any] | None:
        current = dict(state)
        updates: dict[str, Any] = {}
        for hook in hooks:
            before = dict(current)
            result = invoke_hook(hook.callable, current, runtime)
            if inspect.isawaitable(result):
                result = await result
            HookMiddleware._merge_result(hook, before, current, updates, result)
        return updates or None

    @staticmethod
    def _merge_result(
        hook: _ResolvedHook,
        before: Mapping[str, Any],
        current: dict[str, Any],
        updates: dict[str, Any],
        result: Mapping[str, Any] | None,
    ) -> None:
        if result is None:
            return
        if not isinstance(result, Mapping):
            raise TypeError(f"{hook.location} must return a mapping or None, got {type(result).__name__}.")
        for key, value in result.items():
            if key not in before or not HookMiddleware._values_equal(before.get(key), value):
                updates[key] = value
            current[key] = value

    @staticmethod
    def _values_equal(left: Any, right: Any) -> bool:
        if left is right:
            return True
        try:
            result = left == right
        except Exception:
            return False
        return result if isinstance(result, bool) else False


class HookConfigCompiler:
    """Compile recommended model hooks and compatible planner hooks from YAML."""

    def __init__(self, config: Mapping[str, Any], models: Mapping[str, BaseChatModel]) -> None:
        self._config = config
        self._models = models

    def compile(self) -> HookMiddleware | None:
        """Return one native hook middleware, or ``None`` when no hooks are configured."""
        hooks_config = self._hooks_config()
        agent_config = self._section(hooks_config, "agent", "HOOKS.agent")
        model_config = self._section(hooks_config, "model", "HOOKS.model")
        nodes_config = self._section(hooks_config, "nodes", "HOOKS.nodes")
        planner_config = self._section(nodes_config, "planner", "HOOKS.nodes.planner")
        self._warn_ignored_nodes(nodes_config)

        agent_pre = self._resolve_phase(agent_config, "pre", "HOOKS.agent.pre")
        agent_post = self._resolve_phase(agent_config, "post", "HOOKS.agent.post")
        planner_pre = self._resolve_phase(planner_config, "pre", "HOOKS.nodes.planner.pre")
        planner_post = self._resolve_phase(planner_config, "post", "HOOKS.nodes.planner.post")
        model_pre = (*planner_pre, *self._resolve_phase(model_config, "pre", "HOOKS.model.pre"))
        model_post = (*planner_post, *self._resolve_phase(model_config, "post", "HOOKS.model.post"))

        if not any((agent_pre, agent_post, model_pre, model_post)):
            return None
        return HookMiddleware(
            agent_pre=agent_pre,
            agent_post=agent_post,
            model_pre=model_pre,
            model_post=model_post,
        )

    def _hooks_config(self) -> Mapping[str, Any]:
        raw = self._config.get("HOOKS", {})
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise ValueError("HOOKS must be a mapping.")
        return raw

    @staticmethod
    def _section(parent: Mapping[str, Any], key: str, location: str) -> Mapping[str, Any]:
        raw = parent.get(key, {})
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise ValueError(f"{location} must be a mapping.")
        return raw

    @staticmethod
    def _warn_ignored_nodes(nodes_config: Mapping[str, Any]) -> None:
        for node_name in nodes_config:
            if str(node_name) != "planner":
                logger.warning("HOOKS.nodes.{} is not supported by the Deep Agents runtime and was ignored.", node_name)

    def _resolve_phase(
        self,
        section: Mapping[str, Any],
        phase: str,
        location: str,
    ) -> tuple[_ResolvedHook, ...]:
        raw = section.get(phase, ())
        if raw is None:
            return ()
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise ValueError(f"{location} must be a list of hooks.")
        return tuple(self._resolve_item(item, f"{location}[{index}]") for index, item in enumerate(raw))

    def _resolve_item(self, item: Any, location: str) -> _ResolvedHook:
        if callable(item):
            hook = self._validate_hook(item, location)
            return _ResolvedHook(callable=hook, location=location)
        if isinstance(item, str):
            hook = self._resolve_callable(item, location)
            return _ResolvedHook(callable=self._validate_hook(hook, location), location=location)
        if not isinstance(item, Mapping):
            raise TypeError(f"{location} must be a hook name, mapping, or callable.")

        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError(f"{location}.name must be non-empty.")
        if item.get("import") is not None:
            raise ValueError(f"{location}.import is not supported; put the dotted callable path in name.")
        hook = self._validate_hook(self._resolve_callable(name, location), location)
        bindings = self._config_bindings(hook, item, location)
        return _ResolvedHook(callable=functools.partial(hook, **bindings) if bindings else hook, location=location)

    @staticmethod
    def _resolve_callable(name: str, location: str) -> HookCallable:
        normalized = str(name).strip()
        if normalized in _REMOVED_FLEX_HOOK_NAMES:
            raise ValueError(
                f"{location}: Flex hook {normalized!r} has not been migrated to native Deep Agents middleware."
            )
        if "." not in normalized:
            raise ValueError(f"{location}: hook {normalized!r} must be a dotted callable path.")
        return import_callable_from_spec(normalized)

    def _config_bindings(
        self,
        hook: HookCallable,
        item: Mapping[str, Any],
        location: str,
    ) -> dict[str, Any]:
        bindings = {str(key): value for key, value in item.items() if key not in {"name", "model", "import"}}
        raw_model = item.get("model")
        if raw_model not in (None, "", {}):
            bindings["model"] = self._resolve_model(raw_model, location)

        parameters = inspect.signature(hook).parameters
        accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
        for key in bindings:
            parameter = parameters.get(key)
            if parameter is None and not accepts_kwargs:
                raise TypeError(f"{location}: hook does not accept configuration field {key!r}.")
            if parameter is not None and parameter.kind != inspect.Parameter.KEYWORD_ONLY:
                raise TypeError(f"{location}: hook configuration field {key!r} must be keyword-only.")
        return bindings

    def _resolve_model(self, raw_model: Any, location: str) -> BaseChatModel:
        if isinstance(raw_model, str):
            slot = raw_model.strip()
        elif isinstance(raw_model, Mapping):
            unsupported = set(raw_model) - {"name", "model"}
            if unsupported:
                raise ValueError(f"{location}.model overrides are not supported yet: {sorted(unsupported)}")
            slot = str(raw_model.get("name") or raw_model.get("model") or "").strip()
        else:
            slot = str(raw_model).strip()
        model = self._models.get(slot)
        if model is None:
            raise ValueError(f"{location}.model references unknown MODEL slot {slot!r}.")
        return model

    @staticmethod
    def _validate_hook(hook: Any, location: str) -> HookCallable:
        if not callable(hook):
            raise TypeError(f"{location} must resolve to a callable.")
        parameters = list(inspect.signature(hook).parameters.values())
        if not parameters or parameters[0].name != "state":
            raise TypeError(f"{location}: first hook parameter must be named 'state'.")
        if len(parameters) >= 2:
            second = parameters[1]
            is_positional = second.kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
            if is_positional and second.name != "runtime":
                raise TypeError(f"{location}: second positional hook parameter must be named 'runtime'.")
        for parameter in parameters[2:]:
            if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                continue
            if parameter.kind != inspect.Parameter.KEYWORD_ONLY or parameter.default is inspect.Parameter.empty:
                raise TypeError(f"{location}: extra hook parameters must be optional keyword-only parameters.")
        return cast("HookCallable", hook)
