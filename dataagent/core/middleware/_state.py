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
# 本文件中的模型可见文案原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""收集 state schema 上的私有字段名。

一个字段算私有，当且仅当它的注解里用的是 langchain 那个
``PrivateStateAttr`` 实例。若干 schema 的结果取并集。
某一个 schema 的注解在运行时解不开时，整份跳过并记一条警告，
其余 schema 照常收集。跳过的那一份不会留下任何私有字段。
"""

import logging
import typing

from langchain.agents.middleware import types as lc_types

logger = logging.getLogger(__name__)

# 解析失败时打给开发者的 WARNING。%s 留给调用点，不要在这里替换。
_UNRESOLVED_SCHEMA_WARNING = (
    "Could not resolve annotations for state schema %s; its "
    "PrivateStateAttr fields will NOT be kept private. Ensure every "
    "name used in those annotations is imported at runtime rather "
    "than only under TYPE_CHECKING."
)


def private_state_field_names(*state_schemas: type[object]) -> frozenset[str]:
    """返回这些 schema 里标成私有的字段名。

    注解用 ``get_type_hints(..., include_extras=True)`` 在 schema 自己的
    模块里解析。``NameError`` / ``TypeError`` / ``AttributeError``
    （含子类）只丢掉当前这一个 schema；其它异常原样抛出，已经收集的名字
    也不再返回。字段名保持 schema 上的原样。
    """
    found: set[str] = set()
    for schema in state_schemas:
        try:
            hints = typing.get_type_hints(schema, include_extras=True)
        except (NameError, TypeError, AttributeError):
            logger.warning(
                _UNRESOLVED_SCHEMA_WARNING,
                getattr(schema, "__qualname__", schema),
            )
            continue
        found.update(name for name, hint in hints.items() if _carries_private_marker(hint))
    return frozenset(found)


def _carries_private_marker(hint: object) -> bool:
    """这个注解是否带有 langchain 的 ``PrivateStateAttr`` 实例。"""
    return _marker_in(hint, lc_types.PrivateStateAttr)


def _marker_in(hint: object, marker: object) -> bool:
    """``Annotated`` 只看元数据；别的泛型从左到右钻进类型参数。"""
    origin = typing.get_origin(hint)
    if origin is typing.Annotated:
        return _metadata_is(typing.get_args(hint), marker)
    if origin is None:
        return False
    return _any_marked(typing.get_args(hint), marker)


def _metadata_is(arguments: tuple[object, ...], marker: object) -> bool:
    """元数据从第 1 个起，用身份比较。``any`` 命中即停，不会去调 ``__eq__``。"""
    return any(item is marker for item in arguments[1:])


def _any_marked(arguments: tuple[object, ...], marker: object) -> bool:
    """从左到右看类型参数。第一项为真就停，异常也从那一项冒出来。"""
    return any(_marker_in(nested, marker) for nested in arguments)
