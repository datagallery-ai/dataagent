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
from typing import Any

import pytest

from dataagent.core.context.context_trajectory import Context, ContextFactory

DATA_NODE_SPECS: list[dict[str, Any]] = [
    {
        "node_type": "Knowledge",
        "create": {
            "knowledge_type": "domain",
            "knowledge_content": "订单支付后 7 天内可申请退款。",
        },
        "update": {"knowledge_content": "订单支付后 15 天内可申请退款。"},
        "required": "knowledge_content",
    },
    {
        "node_type": "Tool",
        "create": {
            "tool_params": '{"query": "refund policy"}',
            "tool_returns": '{"answer": "7 days"}',
        },
        "update": {"tool_returns": '{"answer": "15 days"}'},
        "required": "tool_returns",
    },
    {
        "node_type": "Table",
        "create": {"path": "/tmp/orders.csv"},
        "update": {"path": "/tmp/orders_v2.csv"},
        "required": "path",
    },
    {
        "node_type": "Column",
        "create": {
            "from_table": "Table(orders)",
            "values": {"sample": [1, 2, 3]},
            "supplementary_schemas": {"dtype": "int"},
        },
        "update": {"supplementary_schemas": {"dtype": "float"}},
        "required": "supplementary_schemas",
    },
    {
        "node_type": "File",
        "create": {"path": "/tmp/report.txt", "source": "tool_output"},
        "update": {"source": "user_upload"},
        "required": "source",
    },
    {
        "node_type": "Script",
        "create": {
            "script_content": "print('hello')",
            "script_type": "python",
            "path": "/tmp/script.py",
            "related_data_list": ["Table(orders)"],
        },
        "update": {"script_content": "print('updated')"},
        "required": "related_data_list",
    },
    {
        "node_type": "Skill",
        "create": {"path": "/tmp/skills/refund"},
        "update": {"path": "/tmp/skills/refund_v2"},
        "required": "path",
    },
]


@pytest.fixture()
def context() -> Context:
    ContextFactory.clear_context()
    ctx = ContextFactory.get_context(
        user_id="data-node-crud-user",
        session_id="data-node-crud-session",
        run_id=0,
        sub_id=0,
    )
    ctx.register_query(query="test query", additional_files=[])
    yield ctx
    ContextFactory.clear_context()


@pytest.mark.parametrize("spec", DATA_NODE_SPECS, ids=[spec["node_type"] for spec in DATA_NODE_SPECS])
def test_data_node_crud_round_trip(context: Context, spec: dict[str, Any]):
    node_type = spec["node_type"]
    expected_label = f"{node_type.lower()}00000"
    graph_label = f"{node_type}({expected_label})"

    created_label = context.register_node(
        node_type=node_type,
        description="initial description",
        predecessor_node=["Query(query00000)"],
        edge_type="test_data",
        **spec["create"],
    )

    assert created_label == graph_label

    ir = context.get_IR_from_node(graph_label)
    assert ir.label == expected_label
    assert ir.description == "initial description"
    assert context.get_trajectory().nodes[graph_label]["description"] == "initial description"
    for field, value in spec["create"].items():
        assert getattr(ir, field) == value
        assert context.get_trajectory().nodes[graph_label][field] == value

    changes = {"description": "updated description", **spec["update"]}
    context.modify_node(graph_label, changes)

    updated_ir = context.get_IR_from_node(graph_label)
    assert updated_ir.description == "updated description"
    assert context.get_trajectory().nodes[graph_label]["description"] == "updated description"
    for field, value in spec["update"].items():
        assert getattr(updated_ir, field) == value
        assert context.get_trajectory().nodes[graph_label][field] == value
    assert updated_ir.history[0]["description"] == "initial description"

    context.remove_node(graph_label)

    assert graph_label not in context.get_trajectory().nodes
    assert expected_label not in context._IR._nodes[node_type]
    with pytest.raises(ValueError, match=f"Cannot get IR with name '{expected_label}'"):
        context.get_IR_from_node(graph_label)


@pytest.mark.parametrize("spec", DATA_NODE_SPECS, ids=[spec["node_type"] for spec in DATA_NODE_SPECS])
def test_data_node_required_fields_are_enforced(context: Context, spec: dict[str, Any]):
    node_type = spec["node_type"]
    create_kwargs = dict(spec["create"])
    create_kwargs.pop(spec["required"])

    with pytest.raises(ValueError, match=f"A {node_type} node must contain info"):
        context.register_node(
            node_type=node_type,
            description="missing required field",
            predecessor_node=["Query(query00000)"],
            edge_type="test_data",
            **create_kwargs,
        )


@pytest.mark.parametrize("spec", DATA_NODE_SPECS, ids=[spec["node_type"] for spec in DATA_NODE_SPECS])
def test_data_node_schema_and_full_content(context: Context, spec: dict[str, Any]):
    node_type = spec["node_type"]
    expected_label = f"{node_type.lower()}00000"
    graph_label = f"{node_type}({expected_label})"

    context.register_node(
        node_type=node_type,
        description="readable description",
        predecessor_node=["Query(query00000)"],
        edge_type="test_data",
        **spec["create"],
    )

    ir = context.get_IR_from_node(graph_label)

    assert ir.get_schema() == {
        "label": expected_label,
        "description": "readable description",
    }

    full_content = ir.get_full_content()
    assert full_content["label"] == expected_label
    assert full_content["description"] == "readable description"
    assert full_content["session_id"] == "data-node-crud-session"
    assert full_content["run_id"] == 0
    assert full_content["history"] == {}
    assert "created_at" in full_content
    for field, value in spec["create"].items():
        assert full_content[field] == value
