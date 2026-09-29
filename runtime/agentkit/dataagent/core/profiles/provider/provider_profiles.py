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
"""登记某个 provider 或某个具体模型该怎么被构造出来。

这里只处理 ``init_chat_model`` 的静态参数、构造前钩子，以及到了
解析那一刻才算得出来的参数。系统提示、工具清单和中间件属于另一套画像。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .._keys import validate_profile_key

_LOG = logging.getLogger(__name__)

# 开发者日志，不是模型上下文。句子按上游逐字保留，空格也保留。
_MERGE_NOTICE = "Merging ProviderProfile under %r on top of existing registration; init_kwargs and factory outputs merge with the new profile winning on shared keys, and pre_init callables chain."
_EMPTY_SPEC = "Empty model spec; no ProviderProfile lookup performed."
_BLANK_HALF = "Model spec %r has an empty provider or model half; no ProviderProfile lookup performed."
_EXACT_MISS = "No exact ProviderProfile for %r; using provider %r profile."
_APPLY_MISS = "No provider profile matched spec %r; building the model with caller kwargs only. Registry keys are matched exactly, so check the provider and model identifier."
_BASE_PRE_FAILED = "Base pre_init in chained ProviderProfile raised for spec %r; override pre_init will not run."
_OVERRIDE_PRE_FAILED = "Override pre_init in chained ProviderProfile raised for spec %r."
_BASE_FACTORY_FAILED = "Base init_kwargs_factory in chained ProviderProfile raised; override factory will not run."
_OVERRIDE_FACTORY_FAILED = "Override init_kwargs_factory in chained ProviderProfile raised."


@dataclass(frozen=True)
class ProviderProfile:
    """一份画像：静态 kwargs、可选的前置钩子、可选的动态 kwargs 工厂。

    ``init_kwargs`` 在构造后是只读视图。传入普通 dict 时只浅拷贝顶层；
    传入已经包好的 ``MappingProxyType`` 时连顶层也不拷，调用方仍握着底层
    dict 就能改到这份画像。嵌套的 dict 和 list 始终共享。
    """

    init_kwargs: Mapping[str, Any] = field(default_factory=dict)
    pre_init: Callable[[str], None] | None = None
    init_kwargs_factory: Callable[[], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        """还不是只读视图时，拷一层顶层再包上。"""
        current = self.init_kwargs
        if isinstance(current, MappingProxyType):
            return
        frozen_view = MappingProxyType(dict(current))
        object.__setattr__(self, "init_kwargs", frozen_view)


_PROVIDER_PROFILES: dict[str, ProviderProfile] = {}


def _ensure_provider_profiles_loaded() -> None:
    """每次注册或有效查找都再问一次引导函数。本模块不记「已经加载」。"""
    from .._builtin_profiles import _ensure_builtin_profiles_loaded

    _ensure_builtin_profiles_loaded()


def _register_provider_profile_impl(key: str, profile: ProviderProfile) -> None:
    """写入注册表。不负责引导。键已经有画像时叠加上去。"""
    validate_profile_key(key)
    previous = _PROVIDER_PROFILES.get(key)
    if previous is None:
        _PROVIDER_PROFILES[key] = profile
        return
    _LOG.info(_MERGE_NOTICE, key)
    _PROVIDER_PROFILES[key] = _merge_provider_profiles(previous, profile)


def register_provider_profile(key: str, profile: ProviderProfile) -> None:
    """先引导，再登记。非法键也会先走一遍引导，然后原样把校验异常抛出去。"""
    _ensure_provider_profiles_loaded()
    _register_provider_profile_impl(key, profile)


def _spec_is_blank(spec: str) -> bool:
    return not spec


def _split_spec(spec: str) -> tuple[str, str, str]:
    return spec.partition(":")


def _half_missing(provider: str, separator: str, model: str) -> bool:
    return bool(separator) and (not provider or not model)


def _stored_provider(provider: str, separator: str) -> ProviderProfile | None:
    if not separator:
        return None
    return _PROVIDER_PROFILES.get(provider)


def get_provider_profile(spec: str) -> ProviderProfile | None:
    """按整串精确命中，否则在有冒号时回落到 provider。两边都有就现合并，不写回。"""
    if _spec_is_blank(spec):
        _LOG.debug(_EMPTY_SPEC)
        return None
    provider, separator, model = _split_spec(spec)
    if _half_missing(provider, separator, model):
        _LOG.debug(_BLANK_HALF, spec)
        return None
    _ensure_provider_profiles_loaded()
    exact = _PROVIDER_PROFILES.get(spec)
    provider_profile = _stored_provider(provider, separator)
    if exact is not None and provider_profile is not None:
        return _merge_provider_profiles(provider_profile, exact)
    if exact is not None:
        return exact
    if provider_profile is None:
        return None
    _LOG.debug(_EXACT_MISS, spec, provider)
    return provider_profile


def _chain_pre_init(earlier: Callable[[str], None], later: Callable[[str], None]) -> Callable[[str], None]:
    """先跑原来的钩子。它抛 Exception 时后面的钩子不跑，异常原样继续往外走。"""

    def chained(spec: str) -> None:
        try:
            earlier(spec)
        except Exception:
            _LOG.exception(_BASE_PRE_FAILED, spec)
            raise
        try:
            later(spec)
        except Exception:
            _LOG.exception(_OVERRIDE_PRE_FAILED, spec)
            raise

    return chained


def _chain_factories(
    earlier: Callable[[], dict[str, Any]],
    later: Callable[[], dict[str, Any]],
) -> Callable[[], dict[str, Any]]:
    """两个工厂都调用。前一个的返回值必须能用 ``**`` 展开。"""

    def chained() -> dict[str, Any]:
        try:
            combined = {**earlier()}
        except Exception:
            _LOG.exception(_BASE_FACTORY_FAILED)
            raise
        try:
            combined.update(later())
        except Exception:
            _LOG.exception(_OVERRIDE_FACTORY_FAILED)
            raise
        return combined

    return chained


def _pick_pre_init(base: ProviderProfile, override: ProviderProfile) -> Callable[[str], None] | None:
    earlier = base.pre_init
    later = override.pre_init
    if earlier is not None and later is not None:
        return _chain_pre_init(earlier, later)
    return later or earlier


def _pick_factory(
    base: ProviderProfile,
    override: ProviderProfile,
) -> Callable[[], dict[str, Any]] | None:
    earlier = base.init_kwargs_factory
    later = override.init_kwargs_factory
    if earlier is not None and later is not None:
        return _chain_factories(earlier, later)
    return later or earlier


def _merge_provider_profiles(base: ProviderProfile, override: ProviderProfile) -> ProviderProfile:
    """override 盖在 base 上。只有一边有钩子或工厂时，保留原来的函数对象。"""
    layered = {**base.init_kwargs, **override.init_kwargs}
    return ProviderProfile(
        init_kwargs=layered,
        pre_init=_pick_pre_init(base, override),
        init_kwargs_factory=_pick_factory(base, override),
    )


def _copied_caller_kwargs(kwargs: Mapping[str, Any] | None) -> dict[str, Any]:
    if not kwargs:
        return {}
    return dict(kwargs)


def _invoke_pre_init(profile: ProviderProfile, spec: str, run_pre_init: bool) -> None:
    hook = profile.pre_init
    if run_pre_init and hook is not None:
        hook(spec)


def _layered_init_kwargs(profile: ProviderProfile, caller: dict[str, Any]) -> dict[str, Any]:
    merged = dict(profile.init_kwargs)
    factory = profile.init_kwargs_factory
    if factory is not None:
        merged.update(factory())
    merged.update(caller)
    return merged


def apply_provider_profile(spec: str, kwargs: Mapping[str, Any] | None = None, *, run_pre_init: bool = True) -> dict[str, Any]:
    """先抄下调用方参数，再查找。钩子改到原 dict 上，不会改这份拷贝。"""
    copied = _copied_caller_kwargs(kwargs)
    profile = get_provider_profile(spec)
    if profile is None:
        _LOG.debug(_APPLY_MISS, spec)
        return copied
    _invoke_pre_init(profile, spec, run_pre_init)
    return _layered_init_kwargs(profile, copied)
