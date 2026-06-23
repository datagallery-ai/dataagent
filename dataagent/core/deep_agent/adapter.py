# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Central YAML-to-OpenJiuWen DeepAgent adapter."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from dataagent.core.deep_agent.builders.access import (
    SysOperationBinding,
    WorkspaceAccessPolicy,
    build_sys_operations,
)
from dataagent.core.deep_agent.builders.context import build_context_processor_rail
from dataagent.core.deep_agent.builders.hitl import build_hitl_rail
from dataagent.core.deep_agent.builders.skills import SkillRailBinding, build_skill_rail
from dataagent.core.deep_agent.builders.tools import (
    A2AAgentBinding,
    build_a2a_agents,
    build_local_tools,
    build_mcp_servers,
    register_a2a_agents,
    stop_a2a_agents,
    unregister_a2a_agents,
)
from dataagent.core.deep_agent.builders.workspace import build_workspace
from dataagent.core.deep_agent.plan import DeepAgentBuildPlan
from dataagent.core.deep_agent.spec import DeepAgentBuildSpec
from dataagent.core.deep_agent.tool_builder import build_harness_tools
from dataagent.utils.log import logger

if TYPE_CHECKING:
    from openjiuwen.core.foundation.tool import McpServerConfig, Tool, ToolCard
    from openjiuwen.core.sys_operation import SysOperation


class DeepAgentAdapter:
    """Translate DataAgent configuration into OpenJiuWen build contributions."""

    def __init__(self, config: Any):
        self.config = config
        self.spec = DeepAgentBuildSpec.from_config(config)
        self._diagnostics_logged = False

    def _log_diagnostics(self) -> None:
        if self._diagnostics_logged:
            return
        for diagnostic in self.spec.diagnostics:
            logger.warning(diagnostic)
        self._diagnostics_logged = True

    def build_tools(
        self,
        sys_operation: SysOperation,
        *,
        language: str = "cn",
        read_sys_operation: SysOperation | None = None,
    ) -> list[Tool | ToolCard]:
        self._log_diagnostics()

        plan = DeepAgentBuildPlan()
        plan.add_tools(
            build_harness_tools(
                sys_operation,
                language=language,
                read_sys_operation=read_sys_operation,
                bash_allowlist=self.spec.bash_allowlist,
            ),
            source="OpenJiuWen harness",
        )
        plan.add_tools(build_local_tools(self.spec.local_tools), source="TOOLS.local_functions")
        return plan.tools

    def build_mcps(self) -> list[McpServerConfig]:
        """Build MCP resources for ``create_deep_agent(mcps=...)``."""
        self._log_diagnostics()

        plan = DeepAgentBuildPlan()
        plan.add_mcps(build_mcp_servers(self.spec.mcp_servers), source="TOOLS.mcp_servers")
        return plan.mcps

    def build_a2a_agents(self) -> list[A2AAgentBinding]:
        """Build A2A RemoteAgents for post-create registration."""
        self._log_diagnostics()

        plan = DeepAgentBuildPlan()
        plan.add_a2a_agents(build_a2a_agents(self.spec.a2a_agents), source="TOOLS.A2A")
        return plan.a2a_agents

    def build_skill_rail(
        self,
        sys_operation: SysOperation,
        *,
        access_policy: WorkspaceAccessPolicy | None = None,
    ) -> SkillRailBinding | None:
        """Build the explicit Jiuwen SkillUseRail for builtin/custom/user skills."""
        self._log_diagnostics()
        base_read_roots = ()
        if access_policy is not None:
            base_read_roots = (
                access_policy.workspace_root,
                *access_policy.allow_read_roots,
            )
        return build_skill_rail(
            self.spec.skills,
            sys_operation=sys_operation,
            base_read_roots=base_read_roots,
        )

    def build_hitl_rail(self) -> Any | None:
        """Build Jiuwen's ask_user rail from the legacy YAML switch."""
        self._log_diagnostics()
        return build_hitl_rail(self.spec.enable_human_feedback)

    def build_context_processor_rail(self) -> Any:
        """Build Jiuwen preset context processors with legacy thresholds."""
        self._log_diagnostics()
        return build_context_processor_rail(self.spec.context_compression)

    def build_access_policy(self, workspace_root: str | Path) -> WorkspaceAccessPolicy:
        """Build canonical workspace read/write roots."""
        return WorkspaceAccessPolicy.from_config(
            self.config,
            workspace_root=workspace_root,
            skills=self.spec.skills,
        )

    def build_sys_operations(
        self,
        policy: WorkspaceAccessPolicy,
        *,
        agent_name: str,
    ) -> SysOperationBinding:
        return build_sys_operations(
            policy,
            agent_name=agent_name,
            shell_allowlist=self.spec.bash_allowlist,
        )

    @staticmethod
    def build_workspace(root_path: str | Path, *, language: str = "cn") -> Any:
        """Build the explicit single-root Jiuwen Workspace."""
        return build_workspace(root_path, language=language)

    @staticmethod
    def register_a2a_agents(
        bindings: list[A2AAgentBinding],
        deep_agent: Any,
    ) -> list[A2AAgentBinding]:
        return register_a2a_agents(bindings, deep_agent)

    @staticmethod
    def unregister_a2a_agents(
        bindings: list[A2AAgentBinding],
        deep_agent: Any | None = None,
    ) -> None:
        unregister_a2a_agents(bindings, deep_agent=deep_agent)

    @staticmethod
    async def stop_a2a_agents(bindings: list[A2AAgentBinding]) -> None:
        await stop_a2a_agents(bindings)
