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
from typing import cast

from dataagent.core.context.context_trajectory import ContextFactory
from dataagent.core.context.contextIR import ActionNode


class TestContext:
    """Context类接口测试"""

    def setup_class(self):
        """Context中添加基本信息"""
        context = ContextFactory.get_context(
            user_id="jiutian_applicationlayer", session_id="#00001", run_id=0, sub_id=0
        )
        context.register_query(query="12+23等于几?", additional_files=[])
        context.register_node(
            node_type="Action",
            description="计算个位的加法结果",
            action="Tool(calculator)",
            params={"formula": "2+3"},
            output=5,
            success=True,
            predecessor_node=["Query(query00000)"],
        )
        context.register_node(
            node_type="Knowledge",
            label="加法规则",
            description="做加法运算的详细规则",
            knowledge_type="domain",
            knowledge_content="个位数加法之后需要做十位数加法，再做百位数加法，以此类推",
            predecessor_node=["Action(action00000)"],
            edge_type="find_relevant_knowledge",
        )
        context.register_node(
            node_type="Table",
            label="测试表",
            description="测试用的表",
            path="测试路径/还是测试路径/测试表",
            predecessor_node=["Action(action00000)"],
            edge_type="find_relevant_data",
        )
        context.register_node(
            node_type="Column",
            label="测试表-测试列",
            description="测试用的列",
            from_table="Table(存款表)",
            values={},
            supplementary_schemas={},
            predecessor_node=["Table(测试表)"],
            edge_type="find_relevant_data",
        )
        context.register_node(
            node_type="State",
            description="完成了个位的计算",
            state="个位的加法结果为5",
            predecessor_node=["Action(action00000)"],
        )
        context.register_node(
            node_type="Action",
            description="计算十位的加法结果",
            action="Tool(calculator)",
            params={"formula": "1+2"},
            output=3,
            success=True,
            predecessor_node=["Query(query00000)"],
            add_pt=True,
        )
        context.register_node(
            node_type="State",
            description="完成了十位的计算",
            state="十位的加法结果为3",
            predecessor_node=["Action(action00001)"],
        )
        context.register_node(
            node_type="Action",
            description="计算百位的加法结果",
            action="Tool(calculator)",
            params={"formula": ""},
            output=None,
            success=False,
            predecessor_node=["Query(query00000)"],
            add_pt=True,
        )
        context.register_node(
            node_type="State",
            description="完成了百位的计算",
            state="用户query中不涉及到百位的计算，因此计算无效",
            predecessor_node=["Action(action00002)"],
            remove_pt=True,
        )
        context.register_node(
            node_type="Action",
            description="整合计算结果",
            action="Tool(calculator)",
            params={"formula": "3*10+5"},
            output=35,
            success=True,
            predecessor_node=["State(state00000)", "State(state00001)"],
        )
        context.register_node(
            node_type="State",
            description="完成计算，可以回答用户query了",
            state="12+23等于35",
            predecessor_node=["Action(action00003)"],
        )

    def teardown_class(self):
        """销毁Context实例"""
        ContextFactory.clear_context()

    def test_get_active_branch(self):
        """测试get_active_branch接口"""
        context = ContextFactory.get_context(
            user_id="jiutian_applicationlayer", session_id="#00001", run_id=0, sub_id=0
        )
        assert context.get_active_branch() == {"State(state00003)"}

    def test_get_trajectory(self):
        """测试get_trajectory接口"""
        context = ContextFactory.get_context(
            user_id="jiutian_applicationlayer", session_id="#00001", run_id=0, sub_id=0
        )
        assert len(context.get_trajectory(trimmed=True)) == 10
        assert len(context.get_trajectory(trimmed=False)) == 12

    def test_get_IR_from_node(self):
        """测试get_trajectory接口"""
        context = ContextFactory.get_context(
            user_id="jiutian_applicationlayer", session_id="#00001", run_id=0, sub_id=0
        )
        state = context.get_IR_from_node("State(state00003)")
        assert state.description == "完成计算，可以回答用户query了"

    def test_modify_node(self):
        """测试modify_node接口"""
        context = ContextFactory.get_context(
            user_id="jiutian_applicationlayer", session_id="#00001", run_id=0, sub_id=0
        )
        context.modify_node(graph_node_label="Action(action00003)", changes={"output": 18, "success": False})
        assert context._trajectory.nodes["Action(action00003)"]["output"] == 18
        assert not context._trajectory.nodes["Action(action00003)"]["success"]

        action_ir = cast(ActionNode, context._IR._nodes["Action"]["action00003"])
        assert action_ir.output == 18
        assert not action_ir.success

    def test_remove_node(self):
        """测试remove_node接口"""
        context = ContextFactory.get_context(
            user_id="jiutian_applicationlayer", session_id="#00001", run_id=0, sub_id=0
        )
        context.remove_node(graph_node_label="State(state00003)")
        assert "state00003" not in context._IR._nodes["State"]
        assert "State(state00003)" not in context._trajectory.nodes
        assert context._current_pt == {"Action(action00003)"}

    def test_get_next_data_node(self):
        """测试get_next_data_node接口"""
        context = ContextFactory.get_context(
            user_id="jiutian_applicationlayer", session_id="#00001", run_id=0, sub_id=0
        )
        irs = context.get_next_data_node("Action(action00000)")
        assert len(irs) == 3
        flag_column, flag_table, flag_knowledge = False, False, False
        for ir in irs:
            if ir.label == "测试表-测试列":
                flag_column = True
            if ir.label == "测试表":
                flag_table = True
            if ir.label == "加法规则":
                flag_knowledge = True
        assert flag_knowledge
        assert flag_table
        assert flag_column

    def test_get_previous_action_node(self):
        """测试get_previous_action_node接口"""
        context = ContextFactory.get_context(
            user_id="jiutian_applicationlayer", session_id="#00001", run_id=0, sub_id=0
        )
        ir_1 = context.get_previous_action_node("Column(测试表-测试列)")
        assert len(ir_1) == 1
        assert ir_1[0].label == "action00000"

        ir_2 = context.get_previous_action_node("Knowledge(加法规则)")
        assert len(ir_2) == 1
        assert ir_2[0].label == "action00000"
