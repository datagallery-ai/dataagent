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
"""把模型、后端和中间件装配成一张 deep agent 图。

入口是 ``create_deep_agent``。模型必须由调用方给出。画像、工具描述和
通用子代理的正文都不在这个文件里写，这里只决定它们按什么顺序叠上去。
"""

from langchain.agents import AgentState
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage
from langchain_core.messages import SystemMessage
from langgraph.channels.delta import DeltaChannel
from typing import Annotated, Required

from dataagent.core._excluded_middleware import _apply_excluded_middleware
from dataagent.core._excluded_middleware import _validate_excluded_middleware_config
from dataagent.core._excluded_middleware import _verify_excluded_middleware_coverage
from dataagent.core._models import resolve_model
from dataagent.core._tools import _apply_tool_description_overrides
from dataagent.core.backends.state import StateBackend
from dataagent.core.middleware._prompt_caching import append_prompt_caching_middleware
from dataagent.core.middleware._state import private_state_field_names
from dataagent.core.middleware._tool_exclusion import _ToolExclusionMiddleware
from dataagent.core.middleware._utils import append_to_system_message
from dataagent.core.middleware.context._messages_reducer import _messages_delta_reducer
from dataagent.core.middleware.patch_tool_calls import PatchToolCallsMiddleware
from dataagent.core.middleware.summarization import create_summarization_middleware
from dataagent.core.middleware.filesystem._fs_interrupt import _build_interrupt_on_from_permissions
from dataagent.core.middleware.filesystem.filesystem import FilesystemMiddleware
from dataagent.core.middleware.memory import MemoryMiddleware
from dataagent.core.middleware.skills import SkillsMiddleware
from dataagent.core.middleware.subagents.subagents import GENERAL_PURPOSE_SUBAGENT
from dataagent.core.middleware.subagents.subagents import SubAgentMiddleware
from dataagent.core.middleware.subagents.subagents import _is_compiled_subagent
from dataagent.core.middleware.subagents.subagents import _is_forked_subagent
from dataagent.core.profiles.harness.harness_profiles import GeneralPurposeSubagentProfile
from dataagent.core.profiles.harness.harness_profiles import _apply_profile_prompt
from dataagent.core.profiles.harness.harness_profiles import _harness_profile_for_model

# 包版本写死在这里。不要 import dataagent.core._version，那个模块不做。
_DATAAGENT_DEEP_VERSION = "0.1.0.dev0"
_DATAAGENT_INTEGRATION = "dataagent"
_EXPLICIT_MODEL = "model must be passed explicitly"
_BLOCK_GAP = "\n\n"
_RECURSION_LIMIT = 9999
# create_agent 先写进 metadata、合并后仍要拿掉的键。with_config 按键合并，不传它们删不掉。
# lc_agent_name 与上游一样留给调用方，不在这张表里。
_CREATE_AGENT_METADATA_KEYS = frozenset({"ls_integration", "lc_versions"})

_SCAFFOLD_ALIASES = (
    (FilesystemMiddleware, ()),
    (SubAgentMiddleware, ()),
)
_SCAFFOLD_CLASSES = frozenset(middleware_cls for middleware_cls, _aliases in _SCAFFOLD_ALIASES)
_REQUIRED_MIDDLEWARE_NAMES = frozenset(
    label
    for middleware_cls, aliases in _SCAFFOLD_ALIASES
    for label in (middleware_cls.__name__, *aliases)
)


class DeepAgentState(AgentState):
    """messages 走增量通道，每 50 次写入做一次快照。"""

    messages: Required[
        Annotated[
            list[AnyMessage],
            DeltaChannel(_messages_delta_reducer, snapshot_frequency=50),
        ]
    ]


def _require_model(model):
    if model is None:
        raise TypeError(_EXPLICIT_MODEL)


def _string_spec(model):
    if isinstance(model, str):
        return model
    return None


def _running_backend(backend):
    if backend is not None:
        return backend
    return StateBackend()


def _author_prompt(profile, system_prompt):
    base = _apply_profile_prompt(profile, "")
    if system_prompt is None:
        return base
    if isinstance(system_prompt, SystemMessage):
        if not base:
            return system_prompt
        return SystemMessage(
            content_blocks=[
                *system_prompt.content_blocks,
                {"type": "text", "text": f"{_BLOCK_GAP}{base}"},
            ]
        )
    if not base:
        return system_prompt + ""
    return system_prompt + f"{_BLOCK_GAP}{base}"


def _join_interrupts(from_rules, from_caller):
    if not from_rules and not from_caller:
        return None
    merged = {**from_rules}
    if from_caller:
        merged.update(from_caller)
    return merged


def _permission_interrupts(rules):
    return _build_interrupt_on_from_permissions(rules or [])


def _filesystem_layer(backend, profile, permissions):
    return FilesystemMiddleware(
        backend=backend,
        custom_tool_descriptions=profile.tool_description_overrides,
        _permissions=permissions,
    )


def _summary_layer(model, backend):
    return create_summarization_middleware(model, backend)


def _patch_layer():
    return PatchToolCallsMiddleware()


def _skills_layer(backend, sources):
    return SkillsMiddleware(backend=backend, sources=sources)


def _memory_layer(backend, sources):
    return MemoryMiddleware(
        backend=backend,
        sources=sources,
        add_cache_control=True,
    )


def _exclusion_layer(profile):
    if not profile.excluded_tools:
        return None
    return _ToolExclusionMiddleware(excluded=profile.excluded_tools)


def _core_name_set(stack):
    return {item.name for item in stack}


def _place_custom(base, custom, core_names=None):
    """同名替换留在原位；新名字插到 core 之后，没给 core 时接到末尾。"""
    if not custom:
        return list(base)

    present = {item.name for item in base}
    replacements = {}
    fresh = []
    for item in custom:
        if item.name in present:
            replacements[item.name] = item
        else:
            fresh.append(item)

    stacked = list(base)
    for index, item in enumerate(stacked):
        if item.name in replacements:
            stacked[index] = replacements[item.name]
    if not fresh:
        return stacked
    if core_names is None:
        stacked.extend(fresh)
        return stacked

    anchor = -1
    for index, item in enumerate(stacked):
        if item.name in core_names:
            anchor = index
    if anchor < 0:
        anchor = len(stacked) - 1
    insert_at = anchor + 1
    stacked[insert_at:insert_at] = fresh
    return stacked


def _drop_excluded(stack, profile, matched_classes, matched_names):
    return _apply_excluded_middleware(
        stack,
        profile,
        matched_classes=matched_classes,
        matched_names=matched_names,
    )


def _check_profile(profile):
    _validate_excluded_middleware_config(
        profile,
        required_classes=_SCAFFOLD_CLASSES,
        required_names=_REQUIRED_MIDDLEWARE_NAMES,
    )


def _check_coverage(profile, matched_classes, matched_names):
    _verify_excluded_middleware_coverage(
        profile,
        matched_classes,
        matched_names,
        required_classes=_SCAFFOLD_CLASSES,
        required_names=_REQUIRED_MIDDLEWARE_NAMES,
    )


def _fork_prompt(final_prompt, addendum):
    if not addendum:
        return final_prompt
    if isinstance(final_prompt, SystemMessage):
        return append_to_system_message(final_prompt, addendum)
    if not final_prompt:
        return addendum
    return f"{final_prompt}{_BLOCK_GAP}{addendum}"


def _merge_fork_middleware(parent_middleware, own_middleware):
    combined = [*parent_middleware, *own_middleware]
    return list({item.name: item for item in combined}.values())


def _declarative_subagent(
    spec,
    *,
    parent_model,
    parent_tools,
    parent_prompt,
    parent_permissions,
    parent_interrupt,
    parent_skills,
    parent_memory,
    parent_middleware,
    backend,
):
    forked = _is_forked_subagent(spec)
    raw_model = spec.get("model", parent_model)
    sub_model = resolve_model(raw_model)
    sub_spec = _string_spec(raw_model)
    sub_profile = _harness_profile_for_model(sub_model, sub_spec)
    permissions = spec.get("permissions", parent_permissions)

    stack = []
    if forked and parent_skills is not None:
        stack.append(_skills_layer(backend, parent_skills))
    stack.append(_filesystem_layer(backend, sub_profile, permissions))
    stack.append(_summary_layer(sub_model, backend))
    stack.append(_patch_layer())
    own_skills = spec.get("skills")
    if own_skills and not forked:
        stack.append(_skills_layer(backend, own_skills))

    core_names = _core_name_set(stack)
    stack.extend(sub_profile.materialize_extra_middleware())
    append_prompt_caching_middleware(stack)
    if forked and parent_memory is not None:
        stack.append(_memory_layer(backend, parent_memory))

    matched_classes: set = set()
    matched_names: set = set()
    _check_profile(sub_profile)
    stack = _drop_excluded(stack, sub_profile, matched_classes, matched_names)

    custom = list(spec.get("middleware", []))
    if forked and parent_middleware:
        custom = _merge_fork_middleware(parent_middleware, custom)
    stack = _place_custom(stack, custom, core_names)
    stack = _drop_excluded(stack, sub_profile, matched_classes, matched_names)
    _check_coverage(sub_profile, matched_classes, matched_names)
    exclusion = _exclusion_layer(sub_profile)
    if exclusion is not None:
        stack.append(exclusion)

    if "tools" in spec:
        raw_tools = spec.get("tools")
    else:
        raw_tools = parent_tools
    rewritten = _apply_tool_description_overrides(
        raw_tools,
        sub_profile.tool_description_overrides,
    )
    built = {
        **spec,
        "model": sub_model,
        "tools": rewritten or [],
        "middleware": stack,
    }
    if forked:
        built["system_prompt"] = _fork_prompt(parent_prompt, spec.get("system_prompt"))
    else:
        built["system_prompt"] = _apply_profile_prompt(
            sub_profile,
            spec.get("system_prompt", ""),
        )

    interrupt = _join_interrupts(
        _permission_interrupts(permissions),
        spec.get("interrupt_on", parent_interrupt),
    )
    if interrupt is not None:
        built["interrupt_on"] = interrupt
    return built


def _general_purpose_prompt(profile, settings):
    if settings.system_prompt is not None:
        text = settings.system_prompt
        suffix = profile.system_prompt_suffix
        if suffix is not None:
            text = text + _BLOCK_GAP + suffix
        return text
    return _apply_profile_prompt(profile, GENERAL_PURPOSE_SUBAGENT["system_prompt"])


def _should_skip_general_purpose(settings, inline):
    if settings.enabled is False:
        return True
    return any(item["name"] == GENERAL_PURPOSE_SUBAGENT["name"] for item in inline)


def _general_purpose_spec(
    profile,
    inline,
    *,
    parent_model,
    parent_tools,
    parent_permissions,
    parent_interrupt,
    parent_skills,
    parent_middleware,
    backend,
    matched_classes,
    matched_names,
):
    settings = profile.general_purpose_subagent or GeneralPurposeSubagentProfile()
    if _should_skip_general_purpose(settings, inline):
        return None

    stack = [
        _filesystem_layer(backend, profile, parent_permissions),
        _summary_layer(parent_model, backend),
        _patch_layer(),
    ]
    if parent_skills is not None:
        stack.append(_skills_layer(backend, parent_skills))
    stack.extend(profile.materialize_extra_middleware())
    append_prompt_caching_middleware(stack)
    names_before_exclusion = _core_name_set(stack)

    stack = _drop_excluded(stack, profile, matched_classes, matched_names)
    inheritable = [
        item
        for item in (parent_middleware or [])
        if item.name in names_before_exclusion
    ]
    stack = _place_custom(stack, inheritable, None)
    stack = _drop_excluded(stack, profile, matched_classes, matched_names)
    exclusion = _exclusion_layer(profile)
    if exclusion is not None:
        stack.append(exclusion)

    spec = {
        **GENERAL_PURPOSE_SUBAGENT,
        "model": parent_model,
        "tools": parent_tools or [],
        "middleware": stack,
        "system_prompt": _general_purpose_prompt(profile, settings),
    }
    if settings.description is not None:
        spec["description"] = settings.description
    interrupt = _join_interrupts(
        _permission_interrupts(parent_permissions),
        parent_interrupt,
    )
    if interrupt is not None:
        spec["interrupt_on"] = interrupt
    return spec


def _append_async(stack, specs):
    if not specs:
        return
    # 上游此处有 AsyncSubAgentMiddleware（见 docs/deepagents-rewrite/02 §4），我们未实现
    from dataagent.core.middleware.async_subagents import AsyncSubAgentMiddleware

    stack.append(AsyncSubAgentMiddleware(async_subagents=specs))


def _assemble_main(
    *,
    model,
    profile,
    backend,
    skills,
    memory,
    permissions,
    interrupt_on,
    middleware,
    state_schema,
    inline,
    async_specs,
    matched_classes,
    matched_names,
):
    stack = []
    if skills is not None:
        stack.append(_skills_layer(backend, skills))
    stack.append(_filesystem_layer(backend, profile, permissions))
    if inline:
        stack.append(
            SubAgentMiddleware(
                backend=backend,
                subagents=inline,
                task_description=profile.tool_description_overrides.get("task"),
                state_schema=state_schema,
            )
        )
    stack.append(_summary_layer(model, backend))
    stack.append(_patch_layer())
    _append_async(stack, async_specs)

    core_names = _core_name_set(stack)
    stack.extend(profile.materialize_extra_middleware())
    append_prompt_caching_middleware(stack)
    if memory is not None:
        stack.append(_memory_layer(backend, memory))
    interrupt = _join_interrupts(_permission_interrupts(permissions), interrupt_on)
    if interrupt is not None:
        stack.append(HumanInTheLoopMiddleware(interrupt_on=interrupt))

    stack = _drop_excluded(stack, profile, matched_classes, matched_names)
    stack = _place_custom(stack, middleware or [], core_names)
    stack = _drop_excluded(stack, profile, matched_classes, matched_names)
    exclusion = _exclusion_layer(profile)
    if exclusion is not None:
        stack.append(exclusion)
    return stack


def _stamp_private_keys(stack, state_schema):
    schemas = []
    if state_schema is not None:
        schemas.append(state_schema)
    for item in stack:
        contributed = getattr(item, "state_schema", None)
        if contributed is not None:
            schemas.append(contributed)
    keys = private_state_field_names(*schemas)
    for item in stack:
        if isinstance(item, SubAgentMiddleware):
            item.private_state_keys = item.private_state_keys | keys


def _trace_config(agent_name):
    return {
        "recursion_limit": _RECURSION_LIMIT,
        "metadata": {
            "dataagent_integration": _DATAAGENT_INTEGRATION,
            "dataagent_versions": {_DATAAGENT_INTEGRATION: _DATAAGENT_DEEP_VERSION},
            "lc_agent_name": agent_name,
        },
    }


def _drop_create_agent_metadata(graph):
    """合并之后的主代理 config 里去掉 create_agent 先写上、我们不保留的键。"""
    config = getattr(graph, "config", None)
    if not isinstance(config, dict):
        return graph
    metadata = config.get("metadata")
    if not isinstance(metadata, dict):
        return graph
    config["metadata"] = {key: value for key, value in metadata.items() if key not in _CREATE_AGENT_METADATA_KEYS}
    return graph


def _compile(model, prompt, tools, stack, response_format, context_schema, checkpointer, store, debug, name, cache, state_schema):
    chosen_state = DeepAgentState if state_schema is None else state_schema
    forwarded = {
        "system_prompt": prompt,
        "tools": tools,
        "middleware": stack,
        "response_format": response_format,
        "context_schema": context_schema,
        "checkpointer": checkpointer,
        "store": store,
        "debug": debug,
        "name": name,
        "cache": cache,
        "state_schema": chosen_state,
    }
    compiled = create_agent(model, **forwarded)
    return _drop_create_agent_metadata(compiled.with_config(_trace_config(name)))


def create_deep_agent(
    model: str | BaseChatModel,
    tools=None,
    *,
    system_prompt=None,
    middleware=(),
    subagents=None,
    skills=None,
    memory=None,
    permissions=None,
    backend=None,
    interrupt_on=None,
    response_format=None,
    state_schema=None,
    context_schema=None,
    checkpointer=None,
    store=None,
    debug: bool = False,
    name=None,
    cache=None,
):
    """装配一张 deep agent。``model`` 必须显式传入。"""
    _require_model(model)
    spec = _string_spec(model)
    model = resolve_model(model)
    profile = _harness_profile_for_model(model, spec)
    _check_profile(profile)
    matched_classes: set = set()
    matched_names: set = set()

    rewritten_tools = _apply_tool_description_overrides(
        tools,
        profile.tool_description_overrides,
    )
    backend = _running_backend(backend)
    final_prompt = _author_prompt(profile, system_prompt)

    inline = []
    async_specs = []
    for item in subagents or []:
        if "graph_id" in item:
            async_specs.append(item)
            continue
        if _is_compiled_subagent(item):
            inline.append(item)
            continue
        inline.append(
            _declarative_subagent(
                item,
                parent_model=model,
                parent_tools=tools,
                parent_prompt=final_prompt,
                parent_permissions=permissions,
                parent_interrupt=interrupt_on,
                parent_skills=skills,
                parent_memory=memory,
                parent_middleware=middleware,
                backend=backend,
            )
        )

    general = _general_purpose_spec(
        profile,
        inline,
        parent_model=model,
        parent_tools=rewritten_tools,
        parent_permissions=permissions,
        parent_interrupt=interrupt_on,
        parent_skills=skills,
        parent_middleware=middleware,
        backend=backend,
        matched_classes=matched_classes,
        matched_names=matched_names,
    )
    if general is not None:
        inline.insert(0, general)

    stack = _assemble_main(
        model=model,
        profile=profile,
        backend=backend,
        skills=skills,
        memory=memory,
        permissions=permissions,
        interrupt_on=interrupt_on,
        middleware=middleware,
        state_schema=state_schema,
        inline=inline,
        async_specs=async_specs,
        matched_classes=matched_classes,
        matched_names=matched_names,
    )
    _stamp_private_keys(stack, state_schema)
    _check_coverage(profile, matched_classes, matched_names)
    return _compile(
        model,
        final_prompt,
        rewritten_tools,
        stack,
        response_format,
        context_schema,
        checkpointer,
        store,
        debug,
        name,
        cache,
        state_schema,
    )
