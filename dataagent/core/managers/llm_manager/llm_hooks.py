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
"""LLM 调用 Hook 框架。

设计原则
--------
- 标记与注册分离：@before_hook / @after_hook 只打标签，不执行，不注册。
- 显式注册生效：hooks.register(fn) 决定何时、是否启用某个 hook。
- 执行顺序直觉：before 和 after 均按注册顺序执行，无逆序。

使用示例
--------

    # step 1：在任意模块定义并标记（不会自动执行）
    @before_hook
    def log_input(messages, kwargs):
        print(f"[before] 消息数={len(messages)}")
        return messages, kwargs

    @after_hook
    def log_output(response, messages, kwargs):
        print(f"[after] tokens={response.usage_metadata}")
        return response

    # step 2：初始化时显式注册
    hooks = LLMHooks()
    hooks.register(log_input, log_output)

    # step 3：挂载到 adapter（with_hooks 返回新实例，原实例不受影响）
    adapter = raw_adapter.with_hooks(hooks)
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from dataagent.core.managers.llm_manager.adapters import LLMResponse

# before hook 签名：(messages, kwargs) -> (messages, kwargs)
BeforeHook = Callable[[Any, dict[str, Any]], tuple[Any, dict[str, Any]]]
# after hook 签名：(response, messages, kwargs) -> response
AfterHook = Callable[["LLMResponse", Any, dict[str, Any]], "LLMResponse"]

_HOOK_TYPE_ATTR = "__llm_hook_type__"


def before_hook(fn: BeforeHook) -> BeforeHook:
    """标记函数为 before hook（仅打标签，不注册，不执行）。"""
    setattr(fn, _HOOK_TYPE_ATTR, "before")
    return fn


def after_hook(fn: AfterHook) -> AfterHook:
    """标记函数为 after hook（仅打标签，不注册，不执行）。"""
    setattr(fn, _HOOK_TYPE_ATTR, "after")
    return fn


class LLMHooks:
    """LLM 调用 Hook 注册器。

    调用约定：
    - before hooks 在 LLM 调用前依次执行，可修改 messages / kwargs。
    - after hooks  在 LLM 调用后依次执行，可修改 response。
    - 两者均按注册顺序执行。
    """

    def __init__(self) -> None:
        self._before: list[BeforeHook] = []
        self._after: list[AfterHook] = []

    def __bool__(self) -> bool:
        """有任意已注册的 hook 时为 True，可直接 if hooks 判断。"""
        return bool(self._before or self._after)

    # ── 主注册入口：需配合 @before_hook / @after_hook 标记使用 ──────────────

    def register(self, *fns: Callable) -> "LLMHooks":
        """注册已标记的 hook 函数，按传入顺序依次注册，返回 self 支持链式调用。

        每个函数必须事先用 @before_hook 或 @after_hook 标记，否则抛出 TypeError。
        """
        for fn in fns:
            hook_type: Literal["before", "after"] | None = getattr(fn, _HOOK_TYPE_ATTR, None)
            if hook_type == "before":
                self._before.append(fn)  # type: ignore[arg-type]
            elif hook_type == "after":
                self._after.append(fn)  # type: ignore[arg-type]
            else:
                raise TypeError(f"函数 '{fn.__name__}' 未标记 hook 类型，请先用 @before_hook 或 @after_hook 装饰。")
        return self

    # ── 执行（由 LangChainChatModelAdapter 内部调用）─────────────────────────

    def run_before(self, messages: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """依次执行所有 before hooks，返回最终的 (messages, kwargs)。"""
        for fn in self._before:
            messages, kwargs = fn(messages, kwargs)
        return messages, kwargs

    def run_after(self, response: "LLMResponse", messages: Any, kwargs: dict[str, Any]) -> "LLMResponse":
        """依次执行所有 after hooks，返回最终的 response。"""
        for fn in self._after:
            response = fn(response, messages, kwargs)
        return response
