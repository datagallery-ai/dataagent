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
"""LangGraph-native graph assembly for the nl2sql workflow.

The routing rules of the legacy ``NL2SQLRouter`` are plain graph edges; node
construction (previously ``NL2SQLAgent.from_config``) lives here so the graph
receives already-validated parameters, a host-resolved model and the host-owned
semantic client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from nl2sql.config_manager import NL2SQLConfig
from nl2sql.constants import (
    DEFAULT_NL2SQL_REF_RETRIES,
    DEFAULT_NL2SQL_SEL_RETRIES,
    DEFAULT_WORKFLOW_RECURSION_LIMIT,
)
from nl2sql.nodes import (
    BaseNL2SQLNode,
    ExecutorNode,
    GeneratorNode,
    PerceptorNode,
    ReflectorNode,
    SelectorNode,
    ValidatorNode,
)
from nl2sql.state import NL2SQLState

if TYPE_CHECKING:
    from dataagent.semantic_client import SemanticServiceClient

# 节点顺序与前缀链规则由 NL2SQLConfig.enabled_nodes 校验；
# reflector/selector 是仅有的两个条件路由节点。
_NODE_CLASSES: dict[str, type[BaseNL2SQLNode]] = {
    "perceptor": PerceptorNode,
    "generator": GeneratorNode,
    "validator": ValidatorNode,
    "reflector": ReflectorNode,
    "executor": ExecutorNode,
    "selector": SelectorNode,
}


def build_nl2sql_graph(
    config: NL2SQLConfig,
    *,
    model: BaseChatModel,
    semantic_client: SemanticServiceClient | None = None,
) -> CompiledStateGraph:
    """Assemble the nl2sql StateGraph from validated parameters.

    Args:
        config: Validated plugin parameters; node sections both enable their
            node (prefix chain, see :attr:`NL2SQLConfig.enabled_nodes`) and
            supply its options.
        model: Chat model resolved by the host factory; bound to every node.
        semantic_client: Host-owned semantic-service client injected through
            ``SubagentServices``; ``None`` is valid when the schema is user-supplied.

    Returns:
        The compiled langgraph workflow.
    """
    enabled_nodes = config.enabled_nodes
    security_enabled = bool(config.section("validator").get("sql_security_enabled", False))
    if security_enabled and "reflector" not in enabled_nodes:
        raise ValueError("reflector is required when validator.sql_security_enabled is true.")
    graph = StateGraph(NL2SQLState)
    for name in enabled_nodes:
        node = _build_node(config, name, security_enabled=security_enabled)
        node.bind_model(model)
        if isinstance(node, PerceptorNode):
            node.bind_semantic_client(semantic_client)
        graph.add_node(name, node.run)
    graph.set_entry_point(enabled_nodes[0])

    def next_of(current: str) -> str:
        index = enabled_nodes.index(current)
        return enabled_nodes[index + 1] if index + 1 < len(enabled_nodes) else END

    for name in enabled_nodes:
        following = next_of(name)
        if name == "reflector":
            # proceed → 下一节点；否则回到 validator 重新校验反思结果。
            graph.add_conditional_edges(
                name,
                lambda state, _next=following: _next if state.get("proceed") else "validator",
            )
        elif name == "selector":
            # proceed（置信度达标或重试用尽）→ 结束；否则回到 reflector。
            graph.add_conditional_edges(
                name,
                lambda state: END if state.get("proceed") else "reflector",
            )
        elif following is not END:
            graph.add_edge(name, following)
    return graph.compile()


def _build_node(config: NL2SQLConfig, name: str, *, security_enabled: bool) -> BaseNL2SQLNode:
    """Instantiate one node with its section options."""
    kwargs = dict(config.section(name))
    if name == "reflector" and security_enabled:
        kwargs.update({"sql_security_enabled": True})
    return _NODE_CLASSES[name](config=config, **kwargs)


def state_defaults(config: NL2SQLConfig) -> dict[str, Any]:
    """Per-run initial state values derived from node parameters."""
    return {
        "ref_retries": config.section("reflector").get("ref_retries", DEFAULT_NL2SQL_REF_RETRIES),
        "sel_retries": config.section("selector").get("sel_retries", DEFAULT_NL2SQL_SEL_RETRIES),
    }


def default_run_config() -> dict[str, Any]:
    """LangGraph run config carrying the legacy default recursion limit."""
    return {"recursion_limit": DEFAULT_WORKFLOW_RECURSION_LIMIT}
