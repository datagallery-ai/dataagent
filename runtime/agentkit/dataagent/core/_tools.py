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
"""从调用方交给装配层的工具上读出名字，并按映射改写描述。

dict 与 ``BaseTool`` 会先做副本再改 ``description``。普通 callable
即使带了 ``name`` 属性也原样放回，不为了换一句描述去包一层新工具。
``tools is None`` 时直接返回 ``None``，空序列则返回一个新的空列表。
"""

from typing import Any

from langchain_core.tools import BaseTool


def _tool_name(tool: Any) -> str | None:
    """读工具名字。dict 只认 ``name`` 键，其它对象只认 ``name`` 属性。

    ``str`` 子类原样返回。空字符串算合法名字。读不到、或读到的不是
    ``str``，就返回 ``None``。属性读取抛出的异常不接住。
    """
    raw_name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
    return raw_name if isinstance(raw_name, str) else None


def _description_override(tool: Any, overrides: Any) -> Any:
    """按名字取覆盖文案。没有名字时不查表；缺键和显式 ``None`` 都当没覆盖。"""
    label = _tool_name(tool)
    if label is None:
        return None
    return overrides.get(label)


def _with_new_description(tool: Any, replacement: Any) -> Any:
    """把描述写到副本上。dict 用浅拷贝，BaseTool 用 ``model_copy``。"""
    if isinstance(tool, dict):
        cloned = tool.copy()
        cloned["description"] = replacement
        return cloned
    if isinstance(tool, BaseTool):
        return tool.model_copy(update={"description": replacement})
    return tool


def _apply_tool_description_overrides(tools: Any, overrides: Any) -> list[Any] | None:
    """返回带描述覆盖的新列表。入参容器本身不改。

    命中覆盖的 dict / ``BaseTool`` 不会改原对象，除非 dict 自己的
    ``copy()`` 返回了自己。未命中的项仍是原来的那个对象。
    """
    if tools is None:
        return None
    rewritten: list[Any] = []
    for tool in tools:
        replacement = _description_override(tool, overrides)
        if replacement is None:
            rewritten.append(tool)
            continue
        rewritten.append(_with_new_description(tool, replacement))
    return rewritten
