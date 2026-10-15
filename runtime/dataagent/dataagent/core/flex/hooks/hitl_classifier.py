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
"""可注册的 HITL 问题分类器框架（机制层，与具体类别解耦）。

框架只提供两阶段分类机制；具体类别、关键词表与修正规则由场景侧注册
（de_agent suite 的 :mod:`...de_agent.hooks.hitl_resolver_assets` 提供
``HitlActionSpec`` 实例，``hitl_auto_resolver`` 挂 override）：

1. 关键词匹配：按注册顺序对问题做小写包含匹配，首个命中的 spec 生效，
   全部未命中回落 ``default_action``；空问题直接回 ``default_action``
2. 修正 override 链：``(question, base_action) -> str | None``，按注册序
   首个返回非 None 的 override 生效（场景侧在此实现依赖问题语义的修正，
   如"混合提问"判定）。override 收到的是原始 question（未小写），
   需要自行 lower；返回 None 表示不修正、交由下一个 override / 保留基类。

注册顺序即关键词匹配优先级——迁移场景类别时必须保持既有顺序
（回放门 182 问分布的行为等价前提）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class HitlActionSpec:
    """一个可注册的 HITL 问题类别：名称 + 触发关键词表。

    keywords 做小写包含匹配（非全词匹配），匹配语义与既有
    ``HITL_ACTION_KEYWORDS`` 的 dict 顺序遍历一致。
    """

    name: str
    keywords: tuple[str, ...]


class HitlQuestionClassifier:
    """两阶段 HITL 问题分类器：关键词匹配 + 修正 override 链。"""

    def __init__(
        self,
        actions: Sequence[HitlActionSpec],
        *,
        default_action: str,
        overrides: Sequence[Callable[[str, str], str | None]] = (),
    ) -> None:
        self._actions = tuple(actions)
        self._default_action = default_action
        self._overrides = tuple(overrides)

    def classify(self, question: str) -> str:
        """分类一个 HITL 问题，返回类别名。"""
        if not question:
            return self._default_action
        q = question.lower()
        action = self._default_action
        for spec in self._actions:
            if any(kw.lower() in q for kw in spec.keywords):
                action = spec.name
                break
        for override in self._overrides:
            result = override(question, action)
            if result is not None:
                return result
        return action
