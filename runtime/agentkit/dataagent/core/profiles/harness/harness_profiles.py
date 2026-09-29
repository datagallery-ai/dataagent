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
# 本文件中的模型可见文案常量原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""运行时画像：prompt、工具、中间件和默认子代理怎么叠在一起。

这里只管注册表和合并。具体模型写什么提示词、厂商怎么构造模型，都不在这个文件里。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain_core.language_models import BaseChatModel

from .._keys import validate_profile_key

logger = logging.getLogger(__name__)

_MERGE_NOTICE = "Merging HarnessProfile under %r on top of existing registration; set and middleware fields union, scalar fields prefer the new value."
_EXACT_MISS = "No exact HarnessProfile for %r; using provider %r profile."
_PREBUILT_EXACT = "Using exact HarnessProfile %r for pre-built model (identifier=%r, provider=%r)."
_PREBUILT_PROVIDER = "No exact HarnessProfile for pre-built model (identifier=%r, provider=%r); using provider defaults."
_TOTAL_MISS = "No harness profile matched %s; using defaults. If you registered a profile for this model, ensure the key matches the model's resolved provider and identifier."
_CANNOT_EXPORT_MIDDLEWARE = (
    "HarnessProfileConfig.from_harness_profile() cannot export "
    "`extra_middleware`. Middleware instances and factories are "
    "runtime-only; keep them in `HarnessProfile`."
)
_SERIALIZE_PREFIX = "HarnessProfileConfig.from_harness_profile() cannot serialize `excluded_middleware` class "
_SERIALIZE_SUFFIX = (
    ": it has no public `serialized_name` alias, and arbitrary class-path serialization is "
    "not currently supported. Either add a `serialized_name: ClassVar[str]` "
    "to the class for stable round-trips, or exclude it by `.name` instead."
)


def _load_builtin_profiles() -> None:
    from .._builtin_profiles import _ensure_builtin_profiles_loaded

    _ensure_builtin_profiles_loaded()


def _scaffolding_label(entry: object) -> str | None:
    """名字在 graph 的必需集合里时，返回要写进错误里的那一截。"""
    from ...graph import _REQUIRED_MIDDLEWARE_NAMES as required_names

    if isinstance(entry, str):
        return f"{entry!r} (string)" if entry in required_names else None
    if isinstance(entry, type) and entry.__name__ in required_names:
        return entry.__name__
    return None


def _format_scaffolding_rejection(violations: list[str]) -> str:
    """把多条脚手架违规收成一条，Config 和运行时画像共用这句。"""
    labels = ", ".join(sorted(set(violations)))
    return (
        "HarnessProfile.excluded_middleware is invalid:\n  - "
        f"required scaffolding cannot be excluded: {labels} "
        "(back filesystem tools, subagent dispatch, and permission "
        "enforcement — use excluded_tools for per-tool visibility or "
        "adjust profile settings instead of stripping scaffolding)"
    )


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"`{field_name}` must be str or None, got {type(value).__name__}")


def _string_map(value: object, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"`{field_name}` must be a mapping, got {type(value).__name__}")
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise TypeError(f"`{field_name}` keys and values must be strings")
        cleaned[key] = item
    return cleaned


def _string_set(value: object, field_name: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise TypeError(f"`{field_name}` must be a list/set of strings, got {type(value).__name__}")
    collected: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise TypeError(f"`{field_name}` entries must be strings, got {type(entry).__name__} ({entry!r})")
        collected.append(entry)
    return frozenset(collected)


def _check_middleware_name(entry: object, field_name: str) -> None:
    if not isinstance(entry, str):
        raise TypeError(f"`{field_name}` entries must be strings, got {type(entry).__name__} ({entry!r})")
    if not entry or entry.isspace():
        raise ValueError(f"`{field_name}` entries must be non-empty, non-whitespace strings")
    if ":" in entry:
        raise ValueError(
            f"`{field_name}` entries must be plain middleware names; class-path (`module:Class`) entries are not currently supported, got {entry!r}."
        )
    if entry.startswith("_"):
        raise ValueError(
            f"`{field_name}` entry {entry!r} cannot start with '_' "
            "(underscore-prefixed names refer to private middleware classes "
            "not part of the public exclusion surface)."
        )


def _collect_scaffolding_errors(entries: object, *, names_only: bool) -> None:
    violations: list[str] = []
    for entry in entries:
        if names_only or isinstance(entry, str):
            _check_middleware_name(entry, "excluded_middleware")
        label = _scaffolding_label(entry)
        if label is not None:
            violations.append(label)
    if violations:
        raise ValueError(_format_scaffolding_rejection(violations))


def _lock_overrides(owner: object) -> None:
    current = owner.tool_description_overrides
    if isinstance(current, MappingProxyType):
        return
    object.__setattr__(owner, "tool_description_overrides", MappingProxyType(dict(current)))


def _config_name(entry: object) -> str:
    if isinstance(entry, str):
        return entry
    alias = getattr(entry, "serialized_name", None)
    if isinstance(alias, str) and alias:
        return alias
    raise ValueError(f"{_SERIALIZE_PREFIX}{entry.__name__!r}{_SERIALIZE_SUFFIX}")


@dataclass(frozen=True)
class GeneralPurposeSubagentProfile:
    """自动补上的 general-purpose 子代理要不要留、描述和提示词换不换。"""

    enabled: bool | None = None
    description: str | None = None
    system_prompt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.enabled is not None:
            payload["enabled"] = self.enabled
        if self.description is not None:
            payload["description"] = self.description
        if self.system_prompt is not None:
            payload["system_prompt"] = self.system_prompt
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GeneralPurposeSubagentProfile:
        unknown = set(data) - _SUBAGENT_FIELDS
        if unknown:
            raise TypeError(f"Unknown keys in GeneralPurposeSubagentProfile dict: {sorted(unknown)}")
        enabled = data.get("enabled")
        description = data.get("description")
        system_prompt = data.get("system_prompt")
        if enabled is not None and not isinstance(enabled, bool):
            raise TypeError(f"`enabled` must be bool or None, got {type(enabled).__name__}")
        if description is not None and not isinstance(description, str):
            raise TypeError(f"`description` must be str or None, got {type(description).__name__}")
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise TypeError(f"`system_prompt` must be str or None, got {type(system_prompt).__name__}")
        return cls(enabled=enabled, description=description, system_prompt=system_prompt)


_SUBAGENT_FIELDS = frozenset(item.name for item in fields(GeneralPurposeSubagentProfile))


@dataclass(frozen=True)
class HarnessProfileConfig:
    """能放进 YAML/JSON 的那一部分画像。没有工厂，也没有中间件实例。"""

    base_system_prompt: str | None = None
    system_prompt_suffix: str | None = None
    tool_description_overrides: Mapping[str, str] = field(default_factory=dict)
    excluded_tools: frozenset[str] = frozenset()
    excluded_middleware: frozenset[str] = frozenset()
    general_purpose_subagent: GeneralPurposeSubagentProfile | None = None

    def __post_init__(self) -> None:
        _lock_overrides(self)
        _collect_scaffolding_errors(self.excluded_middleware, names_only=True)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.base_system_prompt is not None:
            payload["base_system_prompt"] = self.base_system_prompt
        if self.system_prompt_suffix is not None:
            payload["system_prompt_suffix"] = self.system_prompt_suffix
        if self.tool_description_overrides:
            payload["tool_description_overrides"] = _string_map(dict(self.tool_description_overrides), "tool_description_overrides")
        if self.excluded_tools:
            payload["excluded_tools"] = sorted(self.excluded_tools)
        if self.excluded_middleware:
            payload["excluded_middleware"] = sorted(self.excluded_middleware)
        if self.general_purpose_subagent is not None:
            payload["general_purpose_subagent"] = self.general_purpose_subagent.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HarnessProfileConfig:
        unknown = set(data) - _CONFIG_FIELDS
        if unknown:
            raise TypeError(f"Unknown keys in HarnessProfileConfig dict: {sorted(unknown)}")
        subagent = data.get("general_purpose_subagent")
        if subagent is None:
            parsed_subagent = None
        elif isinstance(subagent, Mapping):
            parsed_subagent = GeneralPurposeSubagentProfile.from_dict(subagent)
        else:
            raise TypeError(f"`general_purpose_subagent` must be a mapping, got {type(subagent).__name__}")
        return cls(
            base_system_prompt=_optional_text(data.get("base_system_prompt"), "base_system_prompt"),
            system_prompt_suffix=_optional_text(data.get("system_prompt_suffix"), "system_prompt_suffix"),
            tool_description_overrides=_string_map(data.get("tool_description_overrides"), "tool_description_overrides"),
            excluded_tools=_string_set(data.get("excluded_tools"), "excluded_tools"),
            excluded_middleware=_string_set(data.get("excluded_middleware"), "excluded_middleware"),
            general_purpose_subagent=parsed_subagent,
        )

    def to_harness_profile(self) -> HarnessProfile:
        exported = {
            "base_system_prompt": self.base_system_prompt,
            "system_prompt_suffix": self.system_prompt_suffix,
            "tool_description_overrides": self.tool_description_overrides,
            "excluded_tools": self.excluded_tools,
            "excluded_middleware": frozenset(self.excluded_middleware),
            "general_purpose_subagent": self.general_purpose_subagent,
        }
        return HarnessProfile(**exported)

    @classmethod
    def from_harness_profile(cls, profile: HarnessProfile) -> HarnessProfileConfig:
        extra = profile.extra_middleware
        runtime_only = callable(extra) or (isinstance(extra, tuple) and bool(extra))
        if runtime_only:
            raise ValueError(_CANNOT_EXPORT_MIDDLEWARE)
        return cls(
            base_system_prompt=profile.base_system_prompt,
            system_prompt_suffix=profile.system_prompt_suffix,
            tool_description_overrides=_string_map(dict(profile.tool_description_overrides), "tool_description_overrides"),
            excluded_tools=profile.excluded_tools,
            excluded_middleware=frozenset(_config_name(entry) for entry in profile.excluded_middleware),
            general_purpose_subagent=profile.general_purpose_subagent,
        )


_CONFIG_FIELDS = frozenset(item.name for item in fields(HarnessProfileConfig))


@dataclass(frozen=True)
class HarnessProfile:
    """模型造好之后，这一层要怎么改 agent。"""

    base_system_prompt: str | None = None
    system_prompt_suffix: str | None = None
    tool_description_overrides: Mapping[str, str] = field(default_factory=dict)
    excluded_tools: frozenset[str] = frozenset()
    excluded_middleware: frozenset[type[AgentMiddleware] | str] = frozenset()
    extra_middleware: Sequence[AgentMiddleware] | Callable[[], Sequence[AgentMiddleware]] = ()
    general_purpose_subagent: GeneralPurposeSubagentProfile | None = None

    def __post_init__(self) -> None:
        _lock_overrides(self)
        extra = self.extra_middleware
        if not callable(extra) and not isinstance(extra, tuple):
            object.__setattr__(self, "extra_middleware", tuple(extra))
        _collect_scaffolding_errors(self.excluded_middleware, names_only=False)

    def materialize_extra_middleware(self) -> list[AgentMiddleware]:
        extra = self.extra_middleware
        produced = extra() if callable(extra) else extra
        return list(produced)


def _apply_profile_prompt(profile: HarnessProfile, base_prompt: str) -> str:
    """有 base 就换掉原来的主体；有后缀就隔一行接在后面。空串也算写过。"""
    prompt = base_prompt if profile.base_system_prompt is None else profile.base_system_prompt
    suffix = profile.system_prompt_suffix
    if suffix is None:
        return prompt
    if prompt:
        return prompt + "\n\n" + suffix
    return suffix


def _call_if_needed(middleware):
    if callable(middleware):
        return middleware()
    return middleware


def _merge_middleware(base_middleware: Sequence[AgentMiddleware] | Callable[[], Sequence[AgentMiddleware]], override_middleware: Sequence[AgentMiddleware] | Callable[[], Sequence[AgentMiddleware]]) -> Sequence[AgentMiddleware] | Callable[[], Sequence[AgentMiddleware]]:
    """按具体 type() 替换。有一边是空序列时，直接把另一边交回去。"""
    if not base_middleware or not override_middleware:
        return override_middleware or base_middleware

    def assemble():
        base_items = _call_if_needed(base_middleware)
        override_items = _call_if_needed(override_middleware)
        replacement = {type(item): item for item in override_items}
        placed: set[type] = set()
        ordered = []
        for item in base_items:
            kind = type(item)
            if kind in replacement:
                if kind not in placed:
                    ordered.append(replacement[kind])
                    placed.add(kind)
            else:
                ordered.append(item)
        for item in override_items:
            if type(item) not in placed:
                ordered.append(item)
        return ordered

    return assemble


def _merge_subagents(base, override):
    if base is None or override is None:
        return override if base is None else base
    return GeneralPurposeSubagentProfile(
        enabled=base.enabled if override.enabled is None else override.enabled,
        description=base.description if override.description is None else override.description,
        system_prompt=base.system_prompt if override.system_prompt is None else override.system_prompt,
    )


def _merge_profiles(base: HarnessProfile, override: HarnessProfile) -> HarnessProfile:
    """override 里写过的标量盖住 base；集合取并；中间件按类型换。"""
    return HarnessProfile(
        base_system_prompt=base.base_system_prompt if override.base_system_prompt is None else override.base_system_prompt,
        system_prompt_suffix=base.system_prompt_suffix if override.system_prompt_suffix is None else override.system_prompt_suffix,
        tool_description_overrides={**base.tool_description_overrides, **override.tool_description_overrides},
        excluded_tools=base.excluded_tools | override.excluded_tools,
        excluded_middleware=base.excluded_middleware | override.excluded_middleware,
        extra_middleware=_merge_middleware(base.extra_middleware, override.extra_middleware),
        general_purpose_subagent=_merge_subagents(base.general_purpose_subagent, override.general_purpose_subagent),
    )


_HARNESS_PROFILES: dict[str, HarnessProfile] = {}


def _register_harness_profile_impl(key: str, profile: HarnessProfile | HarnessProfileConfig) -> None:
    """把一条画像写进表。这里不引导内置画像，调用方自己决定要不要先加载。"""
    validate_profile_key(key)
    runtime = profile.to_harness_profile() if isinstance(profile, HarnessProfileConfig) else profile
    current = _HARNESS_PROFILES.get(key)
    if current is not None:
        logger.info(_MERGE_NOTICE, key)
        runtime = _merge_profiles(current, runtime)
    _HARNESS_PROFILES[key] = runtime


def register_harness_profile(key: str, profile: HarnessProfile | HarnessProfileConfig) -> None:
    """登记一条画像。同一个键再登记一次，是叠加上去，不是换成新的。"""
    _load_builtin_profiles()
    _register_harness_profile_impl(key, profile)


def _has_any_harness_profile() -> bool:
    from .. import _builtin_profiles

    _load_builtin_profiles()
    return bool(_HARNESS_PROFILES.keys() - _builtin_profiles._BOOTSTRAP_HARNESS_KEYS)


def _get_harness_profile(spec: str) -> HarnessProfile | None:
    if not spec:
        return None
    provider, separator, model = spec.partition(":")
    if separator and (not provider or not model):
        return None
    _load_builtin_profiles()
    exact = _HARNESS_PROFILES.get(spec)
    provider_profile = _HARNESS_PROFILES.get(provider) if separator else None
    if exact is not None and provider_profile is not None:
        return _merge_profiles(provider_profile, exact)
    if exact is not None:
        return exact
    if provider_profile is not None:
        logger.debug(_EXACT_MISS, spec, provider)
        return provider_profile
    return None


def _report_miss(subject: str) -> None:
    logger.debug(_TOTAL_MISS, subject)


def _harness_profile_for_model(model: BaseChatModel, spec: str | None) -> HarnessProfile:
    """有 spec 就按 spec 找。没有的话，先精确键，再退到 provider。找不到给一个空画像。"""
    from ..._models import get_model_identifier, get_model_provider

    if spec is not None:
        found = _get_harness_profile(spec)
        if found is not None:
            return found
        _report_miss(f"spec {spec!r}")
        return HarnessProfile()

    identifier = get_model_identifier(model)
    provider = get_model_provider(model)
    candidates = []
    if provider and identifier:
        candidates.append(f"{provider}:{identifier}")
    carries_provider = identifier is not None and ":" in identifier
    if carries_provider:
        candidates.append(identifier)
    _load_builtin_profiles()
    for candidate in candidates:
        if candidate in _HARNESS_PROFILES:
            found = _get_harness_profile(candidate)
            if found is not None:
                logger.debug(_PREBUILT_EXACT, candidate, identifier, provider)
                return found
    fallback_keys = [provider] if provider is not None else candidates
    for key in fallback_keys:
        found = _get_harness_profile(key)
        if found is not None:
            logger.debug(_PREBUILT_PROVIDER, identifier, provider)
            return found
    _report_miss(f"pre-built model {type(model).__name__} (identifier={identifier!r}, provider={provider!r})")
    return HarnessProfile()
