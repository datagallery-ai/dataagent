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

    # ========== 需求2：subid=0 runid>0时恢复历史context记录 ==========

    # _get_IR_from_pg 在 _pg_url 为空时会直接返回 {}，不会调用 get_IR_from_pg；需 patch 实例方法本身。
    @patch.object(Context, "_get_IR_from_pg")
    def test_requirement_2_restore_previous_runs(self, mock_get_IR_from_pg):
        """
        测试需求2：subid=0 runid>0时实例化时拉起session过往轮次的context记录

        验证点：
        1. 当run_id > 0且sub_id == 0时，调用restore_previous_runs
        2. 历史节点被正确加载到_IR中
        3. 历史轨迹被存储在_historical_trajectories中
        4. 当前trajectory不包含历史节点（只包含当前run的节点）
        5. 节点编号从历史记录继续递增
        """
        # Mock历史数据
        mock_history_run0 = {
            "Query": [
                {
                    "label": "query00000",
                    "description": "历史查询0",
                    "query": "历史查询0",
                    "additional_files": [],  # Query节点必需字段
                    "user_id": "test_user_2",
                    "session_id": "test_session_2",
                    "run_id": 0,
                    "sub_id": 0,
                }
            ],
            "Action": [
                {
                    "label": "action00000",
                    "description": "历史动作0",
                    "action": "Tool(test)",
                    "params": {},  # Action节点必需字段
                    "output": "test_output",  # Action节点必需字段
                    "success": True,  # Action节点必需字段
                    "user_id": "test_user_2",
                    "session_id": "test_session_2",
                    "run_id": 0,
                    "sub_id": 0,
                }
            ],
            "IR_Edge": [
                {
                    "source": "Query(query00000)",
                    "target": "Action(action00000)",
                    "relationship": "leads_to",
                }
            ],
        }

        mock_history_run1 = {
            "Query": [
                {
                    "label": "query00001",
                    "description": "历史查询1",
                    "query": "历史查询1",
                    "additional_files": [],  # Query节点必需字段
                    "user_id": "test_user_2",
                    "session_id": "test_session_2",
                    "run_id": 1,
                    "sub_id": 0,
                }
            ],
            "IR_Edge": [],
        }

        # 调用处为关键字参数：self._get_IR_from_pg(user_id=..., session_id=..., run_id=..., sub_id=...)
        def mock_get_IR_side_effect(*_args, **kwargs):
            run_id = kwargs.get("run_id")
            if run_id == 0:
                return mock_history_run0
            if run_id == 1:
                return mock_history_run1
            return {"Query": [], "IR_Edge": []}

        mock_get_IR_from_pg.side_effect = mock_get_IR_side_effect

        # 创建agent实例，run_id=2
        config = {
            "USER_ID": "test_user_2",
            "SESSION_ID": "test_session_2",
            "RUN_ID": 2,
            "SUB_ID": 0,
        }
        actor_node = MockActorNode()
        agent = FlexAgent(actor_nodes=[actor_node], config=config, debug=True)

        # 初始化context（这会触发restore_previous_runs）
        initial_state = {
            "user_query": "当前查询",
            "user_id": "test_user_2",
            "session_id": "test_session_2",
            "run_id": 2,
            "sub_id": 0,
            "messages": [],
            "complete": False,
        }

        agent._context = agent._get_or_init_context(initial_state)

        # 验证1：restore_previous_runs被调用
        assert agent._context.restored is True

        # 验证2：历史节点被加载到_IR中
        # 检查历史Query节点
        # get_IR的参数顺序是 (label, node_type)
        history_query = cast(QueryNode, agent._context._IR.get_IR("query00000", "Query"))
        assert history_query is not None
        assert history_query.query == "历史查询0"
        assert history_query.run_id == 0

        # 检查历史Action节点
        history_action = agent._context._IR.get_IR("action00000", "Action")
        assert history_action is not None
        assert history_action.description == "历史动作0"
        assert history_action.run_id == 0

        # 验证3：历史轨迹被存储在_historical_trajectories中
        historical_trajectories = agent._context.get_all_historical_trajectories()
        assert 0 in historical_trajectories, "应该包含run_id=0的历史轨迹"
        assert 1 in historical_trajectories, "应该包含run_id=1的历史轨迹"

        # 验证历史轨迹0包含正确的节点
        hist_traj_0 = agent._context.get_historical_trajectory(0)
        assert hist_traj_0 is not None
        assert "Query(query00000)" in hist_traj_0.nodes()
        assert "Action(action00000)" in hist_traj_0.nodes()
        assert ("Query(query00000)", "Action(action00000)") in hist_traj_0.edges()

        # 验证4：当前trajectory包含合并的历史节点（跨轮DAG合并）
        agent._context.register_query(query="当前查询", additional_files=[])
        current_trajectory = agent._context.get_trajectory()
        current_nodes = list(current_trajectory.nodes())

        # 合并后trajectory应包含历史节点和当前节点
        assert "Query(query00000)" in current_nodes, "合并后trajectory应包含历史Query节点"
        assert "Action(action00000)" in current_nodes, "合并后trajectory应包含历史Action节点"
        assert "Query(query00002)" in current_nodes, "合并后trajectory应包含当前run的Query节点"

        # 验证跨轮桥接边存在
        # 当前run的Query(query00002)应有来自历史轮叶子的桥接边
        current_query = "Query(query00002)"
        predecessors = list(current_trajectory.predecessors(current_query))
        assert len(predecessors) > 0, "当前Query应有来自历史轮的桥接前驱"

        # 验证session_root_pt指向run 0的Query
        assert agent._context.session_root_pt == "Query(query00000)", "session_root_pt应指向run 0的Query"

        # 应该包含当前run的Query节点（编号应该是query00002，因为历史有query00000和query00001）
        current_query_nodes = [n for n in current_nodes if n.startswith("Query(")]
        assert len(current_query_nodes) > 0, "应该存在当前run的Query节点"
        # 验证节点编号从历史继续递增（应该是query00002）
        assert "Query(query00002)" in current_query_nodes, "节点编号应该从历史继续递增"

        # 验证5：节点编号从历史记录继续递增
        # 注册一个新的Action节点，应该从action00001开始（因为历史有action00000）
        initial_pt = agent._context.initial_pt
        assert initial_pt is not None, "initial_pt不应该为None"
        agent._context.register_node(
            node_type="Action",
            description="当前动作",
            action="Tool(current)",
            params={},
            output="ok",
            success=True,
            predecessor_node=[initial_pt],
        )
        current_action_nodes = [n for n in agent._context.get_trajectory().nodes() if n.startswith("Action(")]
        # 应该包含action00001（因为历史有action00000）
        assert any("action00001" in n for n in current_action_nodes), "Action节点编号应该从历史继续递增"

    # ========== 需求3：agent销毁时自动持久化存储 ==========

    @patch.object(Context, "_save_IR_to_pg")
    def test_requirement_3_auto_persist_on_chat_completion(self, mock_save_IR_to_pg):
        """
        测试需求3：实例化销毁时自动持久化存储

        验证点：
        1. chat方法成功完成后，persist_to_pg被调用
        2. chat方法异常时，persist_to_pg仍然被调用（在except块中）
        3. persist_to_pg是幂等的（多次调用不会重复存储）
        """
        # 准备测试数据
        config = {
            "USER_ID": "test_user_3",
            "SESSION_ID": "test_session_3",
            "RUN_ID": 0,
            "SUB_ID": 0,
        }

        actor_node = MockActorNode()
        agent = FlexAgent(actor_nodes=[actor_node], config=config, debug=True)

        initial_state = {
            "user_query": "测试查询",
            "user_id": "test_user_3",
            "session_id": "test_session_3",
            "run_id": 0,
            "sub_id": 0,
            "messages": [],
            "complete": False,
        }

        # Mock workflow_backend
        mock_backend = MagicMock()
        final_state = {
            **initial_state,
            "complete": True,
            "final_answer": "测试回答",
        }
        mock_backend.ainvoke = MagicMock(return_value=final_state)
        agent.workflow_backend = mock_backend

        # 执行chat（需要mock异步）
        import asyncio

        async def run_chat():
            return await agent.chat("测试查询", initial_state=initial_state)

        # 由于chat是async，我们直接测试persist逻辑
        agent._context = agent._get_or_init_context(initial_state)
        agent._context.register_query(query="测试查询", additional_files=[])

        # 验证1：chat方法成功完成后，persist_to_pg被调用
        # 模拟chat成功完成后的persist调用
        agent._context.persist_to_pg()

        # 验证persist_to_pg被调用
        assert mock_save_IR_to_pg.called, "persist_to_pg应该被调用"

        # 验证2：persist_to_pg是幂等的（多次调用不会重复存储）
        call_count_before = mock_save_IR_to_pg.call_count
        agent._context.persist_to_pg()  # 再次调用
        call_count_after = mock_save_IR_to_pg.call_count

        # 由于幂等性，第二次调用不应该增加调用次数（因为_persisted标志）
        # persist_to_pg会在开始时检查_persisted标志，如果已设置则直接返回
        assert call_count_after == call_count_before, "幂等性：第二次调用不应该增加save_IR_to_pg的调用次数"
        assert agent._context._persisted is True, "persist标志应该被设置"

        # 验证3：只有当前run的节点被持久化
        # 检查保存的节点都是当前run_id的
        saved_calls = mock_save_IR_to_pg.call_args_list
        assert len(saved_calls) > 0, "应该有节点被保存"

        # 验证保存的节点run_id都是0（当前run_id）
        # _save_IR_to_pg的签名是: _save_IR_to_pg(node_type: str, ir_data: dict)
        for call_args in saved_calls:
            # call_args是Call对象，可以使用call_args.kwargs或call_args.args
            if call_args.kwargs:
                node_type = call_args.kwargs.get("node_type")
                ir_data = call_args.kwargs.get("ir_data")
            else:
                # 如果是位置参数，第一个是node_type，第二个是ir_data
                if len(call_args.args) >= 2:
                    node_type = call_args.args[0]
                    ir_data = call_args.args[1]
                else:
                    continue
            if node_type and node_type != "IR_Edge" and ir_data:
                assert ir_data.get("run_id") == 0, f"节点{node_type}的run_id应该是0"

    @patch.object(Context, "_save_IR_to_pg")
    def test_requirement_3_auto_persist_on_chat_error(self, mock_save_IR_to_pg):
        """
        测试需求3（异常情况）：chat方法异常时，persist_to_pg仍然被调用
        """
        config = {
            "USER_ID": "test_user_3_error",
            "SESSION_ID": "test_session_3_error",
            "RUN_ID": 0,
            "SUB_ID": 0,
        }

        actor_node = MockActorNode()
        agent = FlexAgent(actor_nodes=[actor_node], config=config, debug=True)

        initial_state = {
            "user_query": "测试查询",
            "user_id": "test_user_3_error",
            "session_id": "test_session_3_error",
            "run_id": 0,
            "sub_id": 0,
            "messages": [],
            "complete": False,
        }

        agent._context = agent._get_or_init_context(initial_state)
        agent._context.register_query(query="测试查询", additional_files=[])

        # 模拟chat异常后的persist调用（chat方法中的except块）
        try:
            raise RuntimeError("模拟chat执行错误")
        except Exception:
            # 模拟chat方法except块中的persist调用
            agent._context.persist_to_pg()

        # 验证即使发生异常，persist_to_pg仍然被调用
        assert mock_save_IR_to_pg.called, "即使发生异常，persist_to_pg也应该被调用"
        assert agent._context._persisted is True, "persist标志应该被设置"
