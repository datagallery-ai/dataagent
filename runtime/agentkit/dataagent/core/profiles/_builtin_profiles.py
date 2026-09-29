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
"""第一次碰画像注册表时，才把内置画像和第三方插件登记进去。

import 本模块只会把画像子模块装进来。``register()`` 和 entry point
发现留在 ``_ensure_builtin_profiles_loaded``。内置阶段抛了 ``Exception``
就把两份注册表按开始时的浅拷贝退回去；单个插件失败只跳过那一个。
``KeyboardInterrupt`` 不走这套回滚。
"""

from __future__ import annotations

import logging
import threading
import warnings
from importlib.metadata import entry_points

from .harness import _anthropic_haiku_4_5, _anthropic_opus_4_7, _anthropic_sonnet_4_6, _nvidia_nemotron_3_ultra, _openai_codex
from .harness.harness_profiles import _HARNESS_PROFILES
from .provider import _openai
from .provider.provider_profiles import _PROVIDER_PROFILES

logger = logging.getLogger(__name__)

_PROVIDER_PROFILE_GROUP = "deepagents.provider_profiles"
"""第三方提供者画像的 entry point 组名。"""

_HARNESS_PROFILE_GROUP = "deepagents.harness_profiles"
"""第三方 harness 画像的 entry point 组名。"""

_BOOTSTRAP_FAILED = "Built-in profile bootstrap failed; restoring pre-bootstrap registry state."

_BUILTIN_ORDER = (
    _openai,
    _anthropic_opus_4_7,
    _anthropic_sonnet_4_6,
    _anthropic_haiku_4_5,
    _nvidia_nemotron_3_ultra,
    _openai_codex,
)

_PLUGIN_GROUPS = (_PROVIDER_PROFILE_GROUP, _HARNESS_PROFILE_GROUP)

_loaded: bool = False
_loading_thread_id: int | None = None
_BOOTSTRAP_HARNESS_KEYS: frozenset[str] = frozenset()
_BOOTSTRAP_CONDITION = threading.Condition()


def _plugin_label(ep):
    """日志里用来指认一个 entry point 的短名字。

    发行版名是非空字符串时一并写上，避免不同包撞了同一个入口名。
    """
    dist = getattr(ep, "dist", None)
    dist_name = None
    if dist is not None:
        dist_name = getattr(dist, "name", None)
    if isinstance(dist_name, str) and dist_name:
        return f"{ep.name!r} (dist={dist_name!r})"
    return repr(ep.name)


def _take_bootstrap_turn() -> bool:
    """当前线程要不要把这一轮引导做完。

    已经做过，或者就是正在做的那条线程，返回 ``False``。
    别的线程正在做时，等到它结束再从头看一遍。
    """
    global _loading_thread_id
    current = threading.get_ident()
    with _BOOTSTRAP_CONDITION:
        while True:
            if _loaded:
                return False
            owner = _loading_thread_id
            if owner == current:
                return False
            if owner is None:
                _loading_thread_id = current
                return True
            _BOOTSTRAP_CONDITION.wait()


def _visit_group(group: str) -> None:
    """跑完一组 entry point。单个插件的失败留在组内。"""
    try:
        found = entry_points(group=group)
    except Exception as exc:
        message = f"Failed to enumerate {group} entry points; no third-party plugins in this group will load: {type(exc).__name__}: {exc}"
        logger.warning(message, exc_info=True)
        warnings.warn(message, stacklevel=2)
        return
    for ep in found:
        plugin_label = _plugin_label(ep)
        try:
            register = ep.load()
        except Exception as exc:
            message = f"Skipping {group} plugin {plugin_label}: failed to load entry point {ep.value!r}: {type(exc).__name__}: {exc}"
            logger.exception(message)
            warnings.warn(message, stacklevel=2)
            continue
        if not callable(register):
            message = f"Skipping {group} plugin {plugin_label}: entry point {ep.value!r} did not resolve to a callable."
            logger.error(message)
            warnings.warn(message, stacklevel=2)
            continue
        try:
            register()
        except Exception as exc:
            message = f"Skipping {group} plugin {plugin_label}: registration callable {ep.value!r} raised: {type(exc).__name__}: {exc}"
            logger.exception(message)
            warnings.warn(message, stacklevel=2)


def _restore_registries(provider_backup, harness_backup) -> None:
    """把两个注册表按引导前的浅拷贝填回去，对象身份不变。"""
    _PROVIDER_PROFILES.clear()
    _PROVIDER_PROFILES.update(provider_backup)
    _HARNESS_PROFILES.clear()
    _HARNESS_PROFILES.update(harness_backup)


def _finish_turn(*, keys, loaded: bool) -> None:
    """放开等在外面的线程。``loaded`` 只在成功时为真。"""
    global _loaded, _BOOTSTRAP_HARNESS_KEYS, _loading_thread_id
    with _BOOTSTRAP_CONDITION:
        _BOOTSTRAP_HARNESS_KEYS = keys
        if loaded:
            _loaded = True
        _loading_thread_id = None
        _BOOTSTRAP_CONDITION.notify_all()


def _ensure_builtin_profiles_loaded() -> None:
    """按固定顺序登记内置画像，再发现两组第三方插件。"""
    if not _take_bootstrap_turn():
        return
    provider_backup = dict(_PROVIDER_PROFILES)
    harness_backup = dict(_HARNESS_PROFILES)
    keys_backup = _BOOTSTRAP_HARNESS_KEYS
    try:
        for builtin in _BUILTIN_ORDER:
            builtin.register()
        for group in _PLUGIN_GROUPS:
            _visit_group(group)
        snapshot = frozenset(_HARNESS_PROFILES)
    except Exception:
        logger.exception(_BOOTSTRAP_FAILED)
        _restore_registries(provider_backup, harness_backup)
        _finish_turn(keys=keys_backup, loaded=False)
        raise
    _finish_turn(keys=snapshot, loaded=True)
