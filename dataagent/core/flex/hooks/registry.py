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
"""Flex hook 解析：内置短名注册表 + ``module.path.callable`` 字符串（与 tool hook 相同）。"""

from __future__ import annotations

from typing import Any

BUILTIN_HOOK_REGISTRY: dict[str, str] = {
    "pre_metadata_tracker": "dataagent.core.flex.hooks.metadata_tracker.pre_metadata_tracker",
    "post_metadata_tracker": "dataagent.core.flex.hooks.metadata_tracker.post_metadata_tracker",
    "pruner": "dataagent.core.flex.hooks.pruner.pruner",
    "portraiter": "dataagent.core.flex.hooks.portraiter.portraiter",
    "cross_session_recall": "dataagent.core.flex.hooks.cross_session_recall.cross_session_recall",
    "context_reference_rewriter": "dataagent.core.flex.hooks.context_reference_rewriter.context_reference_rewriter",
    "organize_workspace": "dataagent.core.flex.hooks.organize_workspace.organize_workspace",
    "human_feedback_guard": "dataagent.core.flex.hooks.human_feedback_guard.human_feedback_guard",
    "intent_understanding": "dataagent.core.flex.hooks.intent_understanding.intent_understanding",
    "semantic_retrieve_context_loader": "dataagent.core.flex.hooks.semantic_retrieve.semantic_retrieve_context_loader",
    "plan_enforcer": "dataagent.core.flex.hooks.plan_enforcer.plan_enforcer",
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
