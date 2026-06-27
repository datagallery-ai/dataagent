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
"""Example hooks for the ``example_suite`` reference Suite.

Hook modules are loaded via single-file import (``import_callable_from_suite_root``).
Do **not** use package-relative imports such as ``from .common import ...`` or
``from hooks.common import ...``; keep helpers in this file or use absolute imports
(e.g. ``dataagent.*``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dataagent.core.cbb.runtime import Runtime


def suite_example_pre(state: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    """
    Planner 前置 demo hook：不修改 state，仅用于验证 Suite hook 合并与前缀解析。

    Flex hook 签名须为 ``(state, runtime)`` 或仅 ``(state)``，不可用 ``**kwargs``。

    Args:
        state: Flex graph state dict.
        runtime: Per-invocation runtime handle.

    Returns:
        原样返回 ``state``。
    """
    _ = runtime
    return state


def suite_example_post(state: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    """
    Executor 后置 demo hook：不修改 state，验证 executor.post 槽位合并与前缀解析。

    Args:
        state: Flex graph state dict.
        runtime: Per-invocation runtime handle.

    Returns:
        原样返回 ``state``。
    """
    _ = runtime
    return state


def suite_example_with_model(state: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    """
    Executor 后置 demo hook（带 ``model:`` 字典项）：验证 Suite hook LLM 槽位注册。

    合并后 ``name`` 为 ``example_suite.hooks.custom_hooks.suite_example_with_model``；
    若需调用 LLM，应使用 ``runtime.llm("example_suite.hooks.custom_hooks.suite_example_with_model")``。

    Args:
        state: Flex graph state dict.
        runtime: Per-invocation runtime handle.

    Returns:
        原样返回 ``state``。
    """
    _ = runtime
    return state
