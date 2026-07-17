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
import inspect
import traceback
from typing import Any

from loguru import logger

from dataagent.core.cbb.base_state import BaseState
from dataagent.core.utils.performance import callable_perf_name, get_current_collector


class BaseNode:
    def __init__(self, name: str, chat_model_name: str | None = None, **kwargs):
        self.name = name
        self.chat_model_name = chat_model_name
        self.config = kwargs
        self.modules: dict[str, Any] = {}
        self.pre_hooks: list = []
        self.post_hooks: list = []

    @classmethod
    def validate_input(cls, state: BaseState | dict[str, Any]) -> bool:
        """Implement validation logic."""
        return True

    @classmethod
    def handle_error(cls, state: BaseState | dict[str, Any], error: Exception) -> BaseState | dict[str, Any]:
        """Implement error handling logic."""
        return state

    @classmethod
    def should_use_async_aprocess(cls, node_cls: type) -> bool:
        """供 workflow 适配层判断：是否应调用 ``aprocess``（含仅实现 ``_aprocess`` 的 flex 节点）。"""
        aprocess_impl = getattr(node_cls, "aprocess", None)
        if callable(aprocess_impl) and aprocess_impl is not cls.aprocess and inspect.iscoroutinefunction(aprocess_impl):
            return True
        _aprocess_impl = getattr(node_cls, "_aprocess", None)
        return (
            callable(_aprocess_impl)
            and _aprocess_impl is not cls._aprocess
            and inspect.iscoroutinefunction(_aprocess_impl)
        )

    def mount_module(self, name: str, module: Any) -> None:
        """Mount a Module instance under the given key."""
        self.modules[name] = module

    def unmount_module(self, name: str) -> None:
        """Remove a mounted module by key."""
        if name in self.modules:
            del self.modules[name]

    def get_module(self, name: str) -> Any:
        """Return a mounted module by key."""
        return self.modules[name]

    def add_pre_hook(self, hook: Any, side: str = "right") -> None:
        """Append (side='right') or prepend (side='left') a pre-processing hook."""
        if side == "left":
            self.pre_hooks.insert(0, hook)
        elif side == "right":
            self.pre_hooks.append(hook)
        else:
            raise ValueError("side must be 'left' or 'right'")

    def add_post_hook(self, hook: Any, side: str = "right") -> None:
        """Append (side='right') or prepend (side='left') a post-processing hook."""
        if side == "left":
            self.post_hooks.insert(0, hook)
        elif side == "right":
            self.post_hooks.append(hook)
        else:
            raise ValueError("side must be 'left' or 'right'")

    def process(self, state: BaseState, runtime: Any = None) -> dict[str, Any] | BaseState:
        """Main entry of node (sync, galatea-style)."""
        from dataagent.core.cbb.base_hook import invoke_hook

        collector = get_current_collector()
        with collector.measure("node", self.name):
            for hook in self.pre_hooks:
                with collector.measure("hook", callable_perf_name(hook), hook_scope="node", hook_phase="pre"):
                    state = invoke_hook(hook, state, runtime)
            state = self._process(state, runtime)
            for hook in self.post_hooks:
                with collector.measure("hook", callable_perf_name(hook), hook_scope="node", hook_phase="post"):
                    state = invoke_hook(hook, state, runtime)
            return state

    async def aprocess(self, state: BaseState, runtime: Any = None) -> dict[str, Any] | BaseState:
        """Main entry of node (async, flex-style).

        与 galatea 的 process(state, runtime) 完全对称：
        pre_hooks → _aprocess → post_hooks。

        flex 节点实现 ``_aprocess``，而非覆盖此方法，即可自动获得 hook 能力。

        返回策略：
        - pre-hooks（如 pruner）可能已修改 state.messages（加入 RemoveMessage + compressed）
        - _aprocess 产生 result（含新 message）
        - 需要将 state.messages（含 RemoveMessage）和 result.messages 拼接后返回，
          这样 add_messages reducer 才能先清空旧消息、再追加压缩摘要和新消息
        - 仅返回 _aprocess 显式提供的键（messages 做合并），避免把 state 中已有的
          reducer 管理字段（如 num_turns）一并返回导致重复累加
        """
        from dataagent.core.cbb.base_hook import invoke_hook

        collector = get_current_collector()
        with collector.measure("node", self.name):
            for hook in self.pre_hooks:
                with collector.measure("hook", callable_perf_name(hook), hook_scope="node", hook_phase="pre"):
                    state = invoke_hook(hook, state, runtime)
            if bool(state.get("complete", False)):
                return dict(state)
            result = await self._aprocess(state, runtime)
            for hook in self.post_hooks:
                with collector.measure("hook", callable_perf_name(hook), hook_scope="node", hook_phase="post"):
                    result = invoke_hook(hook, result, runtime)
            result_msgs = result.get("messages", [])
            if result_msgs and not isinstance(result_msgs, list):
                result_msgs = [result_msgs]
            # 增量追加 node 原始输出到 messages_full.json
            if result_msgs:
                try:
                    from dataagent.core.flex.hooks.history_writer import save_messages_full_for_state

                    save_messages_full_for_state(state, result_msgs, runtime=runtime)
                except Exception:
                    logger.warning(f"[{self.name}] 写入 messages_full.json 失败: {traceback.format_exc()}")
            # 仅构造 messages 合并后的结果；不复制 state 的其他字段，
            # 避免 num_turns 等 Annotated[int, add] 字段被 reducer 重复累加。
            new_messages = [*state.get("messages", []), *result_msgs]
            return {**{k: v for k, v in result.items() if k != "messages"}, "messages": new_messages}

    def reconfig(self, **kwargs):  # noqa: B027
        """hot reconfiguration"""
        pass

    def get_node_info(self) -> dict[str, Any]:
        """Return node configurations"""
        return {
            "name": self.name,
            "type": self.__class__.__name__,
            "chat_model_name": self.chat_model_name,
            "config": self.config,
        }

    def _process(self, state: BaseState, runtime: Any = None) -> dict[str, Any] | BaseState:
        """Override this in galatea-style subclasses to get pre/post hook chains for free."""
        raise NotImplementedError("The sync process is not implemented")

    async def _aprocess(self, state: BaseState, runtime: Any = None) -> dict[str, Any] | BaseState:
        """Override this in flex-style subclasses to get async pre/post hook chains for free."""
        raise NotImplementedError("The async process is not implemented")
