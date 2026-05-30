# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Flex 内置 hook 注册表：YAML 中只写 ``name``，与实现一一对应，不对外暴露自定义 import。"""

from __future__ import annotations

from typing import Any

# 键与 YAML HOOKS ``name`` 一致；值与 :func:`dataagent.utils.import_utils.import_callable_from_spec` 一致
BUILTIN_HOOK_REGISTRY: dict[str, str] = {
    "pre_metadata_tracker": "dataagent.core.flex.hooks.metadata_tracker:pre_metadata_tracker",
    "post_metadata_tracker": "dataagent.core.flex.hooks.metadata_tracker:post_metadata_tracker",
    "pruner": "dataagent.core.flex.hooks.pruner:pruner",
    "portraiter": "dataagent.core.flex.hooks.portraiter:portraiter",
    "cross_session_recall": "dataagent.core.flex.hooks.cross_session_recall:cross_session_recall",
}


def resolve_builtin_hook(name: str) -> Any:
    """按内置 ``name`` 解析可调用 hook；未知名抛 ``ValueError``。"""
    key = str(name or "").strip()
    if not key:
        raise ValueError("hook name must be non-empty")
    path = BUILTIN_HOOK_REGISTRY.get(key)
    if path is None:
        known = ", ".join(sorted(BUILTIN_HOOK_REGISTRY))
        raise ValueError(f"Unknown hook name {key!r}. Built-in hooks: {known}")
    from dataagent.utils.import_utils import import_callable_from_spec

    return import_callable_from_spec(path)
