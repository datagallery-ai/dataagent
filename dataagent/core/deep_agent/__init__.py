# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""DeepAgent integration for DataAgent.

Builds jiuwen ``Model``, ``Tool`` list, and ``system_prompt`` from the
DataAgent YAML configuration, then constructs a ``DeepAgent`` via
``create_deep_agent()``.
"""

from dataagent.core.deep_agent.adapter import DeepAgentAdapter
from dataagent.core.deep_agent.builders.access import (
    SysOperationBinding,
    WorkspaceAccessPolicy,
    build_sys_operations,
)
from dataagent.core.deep_agent.builders.context import build_context_processor_rail
from dataagent.core.deep_agent.builders.hitl import (
    build_hitl_rail,
    build_interactive_input,
)
from dataagent.core.deep_agent.builders.skills import SkillRailBinding, build_skill_rail
from dataagent.core.deep_agent.builders.tools import (
    A2AAgentBinding,
    build_a2a_agents,
    build_local_tools,
    build_mcp_servers,
)
from dataagent.core.deep_agent.builders.workspace import build_workspace
from dataagent.core.deep_agent.checkpoint import (
    CheckpointerSpec,
    build_checkpointer_spec,
    checkpointer_lease,
)
from dataagent.core.deep_agent.model_builder import build_model_from_config
from dataagent.core.deep_agent.prompt_builder import build_system_prompt
from dataagent.core.deep_agent.spec import (
    A2AAgentSpec,
    ContextCompressionSpec,
    DeepAgentBuildSpec,
    LocalToolSpec,
    McpServerSpec,
    SkillSpec,
)
from dataagent.core.deep_agent.tool_builder import build_all_tools, build_business_tools, build_harness_tools

__all__ = [
    "A2AAgentBinding",
    "A2AAgentSpec",
    "DeepAgentAdapter",
    "DeepAgentBuildSpec",
    "CheckpointerSpec",
    "ContextCompressionSpec",
    "LocalToolSpec",
    "McpServerSpec",
    "SkillRailBinding",
    "SkillSpec",
    "SysOperationBinding",
    "WorkspaceAccessPolicy",
    "build_model_from_config",
    "build_checkpointer_spec",
    "build_all_tools",
    "build_harness_tools",
    "build_context_processor_rail",
    "build_hitl_rail",
    "build_interactive_input",
    "build_business_tools",
    "build_a2a_agents",
    "build_local_tools",
    "build_mcp_servers",
    "build_skill_rail",
    "build_sys_operations",
    "build_workspace",
    "build_system_prompt",
    "checkpointer_lease",
]
