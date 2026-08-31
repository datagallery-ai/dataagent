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
from collections.abc import Callable
from typing import Any

from dataagent.core.managers.action_manager.base import BaseTool


class ToolRegistry:
    """工具注册表，每个 ToolManager 实例拥有独立的 ToolRegistry"""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self._functions: dict[str, Callable] = {}

    def register(self, name: str, tool: BaseTool):
        """注册工具"""
        if name in self._tools:
            raise ValueError(f"Tool '{name}' already registered")
        self._tools[name] = tool

    def register_function(self, name: str, func: Callable):
        """注册函数式工具"""
        self._functions[name] = func

    def unregister(self, name: str):
        """注销工具"""
        if name in self._tools:
            del self._tools[name]
        if name in self._functions:
            del self._functions[name]

    def get(self, name: str) -> BaseTool | None:
        """获取工具"""
        return self._tools.get(name)

    def get_function(self, name: str) -> Callable | None:
        """获取函数式工具"""
        return self._functions.get(name)

    def call_function(self, name: str, **kwargs) -> Any:
        """调用函数式工具"""
        func = self.get_function(name)
        if func:
            return func(**kwargs)
        raise ValueError(f"Function tool '{name}' not found")

    def exists(self, name: str) -> bool:
        """检查工具是否存在"""
        return name in self._tools or name in self._functions

    def list_tool_names(self, category: str | None = None) -> list[str]:
        """列出工具名称"""
        if category is None:
            return list(self._tools.keys()) + list(self._functions.keys())
        return [name for name, tool in self._tools.items() if tool.category == category]

    def list_tools(self, category: str | None = None) -> dict[str, BaseTool]:
        """列出工具实例"""
        if category is None:
            return self._tools.copy()
        return {name: tool for name, tool in self._tools.items() if tool.category == category}

    def list_functions(self) -> dict[str, Callable]:
        """列出函数式工具"""
        return self._functions.copy()

    def clear(self):
        """清空注册表"""
        self._tools.clear()
        self._functions.clear()
