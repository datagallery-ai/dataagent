# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Flex hook 解析：内置短名注册表 + ``module.path.callable`` 字符串（与 tool hook 相同）。"""

from __future__ import annotations

from typing import Any

# 键与 YAML HOOKS ``name`` 一致；值与 :func:`dataagent.utils.import_utils.import_callable_from_spec` 一致
BUILTIN_HOOK_REGISTRY: dict[str, str] = {
    "pre_metadata_tracker": "dataagent.core.flex.hooks.metadata_tracker.pre_metadata_tracker",
    "post_metadata_tracker": "dataagent.core.flex.hooks.metadata_tracker.post_metadata_tracker",
    "pruner": "dataagent.core.flex.hooks.pruner.pruner",
    "portraiter": "dataagent.core.flex.hooks.portraiter.portraiter",
    "cross_session_recall": "dataagent.core.flex.hooks.cross_session_recall.cross_session_recall",
    "context_reference_rewriter": "dataagent.core.flex.hooks.context_reference_rewriter.context_reference_rewriter",
    "organize_workspace": "dataagent.core.flex.hooks.organize_workspace.organize_workspace",
}


def resolve_builtin_hook(name: str) -> Any:
    """解析 HOOKS 项为可调用 hook。

    先查 :data:`BUILTIN_HOOK_REGISTRY` 短名（如 ``pruner``）；否则将 ``spec`` 视为
    ``module.path.callable`` 并 :func:`import_callable_from_spec`（与 tool hook 一致）。

    Args:
        name: 内置短名或 ``module.path.callable`` 字符串。

    Returns:
        解析后的 hook 可调用对象。

    Raises:
        ValueError: ``name`` 为空或 dotted path 格式非法。
        ImportError, AttributeError, TypeError: 模块或 callable 无法加载。
    """
    key = str(name or "").strip()
    if not key:
        raise ValueError("hook name must be non-empty")
    from dataagent.utils.import_utils import import_callable_from_spec

    path = BUILTIN_HOOK_REGISTRY.get(key)
    if path is not None:
        return import_callable_from_spec(path)
    return import_callable_from_spec(key)
