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
"""
测试核心需求：
1. subid=0 runid>0时实例化时拉起session过往轮次的context记录
2. 实例化销毁时自动持久化存储
"""

from typing import cast
from unittest.mock import MagicMock, patch

from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.context.context_trajectory import Context, ContextFactory
from dataagent.core.context.contextIR import QueryNode
from dataagent.core.flex.agent import FlexAgent


class MockActorNode(BaseNode):
    """Mock actor node for testing"""

    def __init__(self, name: str = "mock_actor"):
        super().__init__(name=name)

    async def invoke(self, state):
        return state


class TestAgentContextRequirements:
    """测试Agent Context相关的三个核心需求"""

    def setup_method(self):
        """每个测试方法执行前的清理"""
        ContextFactory.clear_context()

    def teardown_method(self):
        """每个测试方法执行后的清理"""
        ContextFactory.clear_context()
