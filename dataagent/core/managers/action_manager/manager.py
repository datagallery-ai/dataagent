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
import asyncio
import inspect
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from dataagent.core.managers.action_manager.base import BaseTool, ToolError, ToolResult, ToolType
from dataagent.core.managers.action_manager.registry import ToolRegistry
from dataagent.core.managers.action_manager.schemas import ToolSchema
from dataagent.utils.constants import (
    DEFAULT_BUILTIN_LOCAL_TOOLS,
    DEFAULT_BUILTIN_SKILL_NAMES,
    DEFAULT_MCP_DISCOVERY_TIMEOUT,
    JOB_SUBAGENT_TOOL_CATALOG_HEADER,
    JOB_SUBAGENT_TOOL_FIXED_CALL_INSTRUCTIONS,
    SUBAGENT_TOOL_CATALOG_HEADER,
    SUBAGENT_TOOL_FIXED_CALL_INSTRUCTIONS,
)
from dataagent.utils.runtime_paths import dataagent_package_path, resolve_user_root

# Always eligible for discovery under `dataagent/actions/skills/` in addition to TOOLS.skills.builtin.
# DEFAULT_BUILTIN_SKILL_NAMES is imported from dataagent.utils.constants.

# Full catalog of code-defined builtin local tools (enabled subset from constants.DEFAULT_BUILTIN_LOCAL_TOOLS).
_BUILTIN_LOCAL_TOOL_CATALOG: dict[str, dict[str, str]] = {
    "bash": {"name": "bash", "function": "bash", "module": "dataagent.actions.tools.local_tool.tools"},
    "edit_file": {"name": "edit_file", "function": "edit_file", "module": "dataagent.actions.tools.local_tool.tools"},
    "read_file": {"name": "read_file", "function": "read_file", "module": "dataagent.actions.tools.local_tool.tools"},
    "write_file": {
        "name": "write_file",
        "function": "write_file",
        "module": "dataagent.actions.tools.local_tool.tools",
    },
    "grep": {"name": "grep", "function": "grep", "module": "dataagent.actions.tools.local_tool.tools"},
    "glob": {"name": "glob", "function": "glob", "module": "dataagent.actions.tools.local_tool.tools"},
    "create_plan": {
        "name": "create_plan",
        "function": "create_plan",
        "module": "dataagent.actions.tools.local_tool.plan",
    },
    "update_plan": {
        "name": "update_plan",
        "function": "update_plan",
        "module": "dataagent.actions.tools.local_tool.plan",
    },
    "delete_plan": {
        "name": "delete_plan",
        "function": "delete_plan",
        "module": "dataagent.actions.tools.local_tool.plan",
    },
    "complete_current_todo": {
        "name": "complete_current_todo",
        "function": "complete_current_todo",
        "module": "dataagent.actions.tools.local_tool.plan",
    },
}


def _builtin_local_tool_specs_from_constants() -> list[dict[str, Any]]:
    """Resolve DEFAULT_BUILTIN_LOCAL_TOOLS against the catalog (intersection by tool name)."""
    specs: list[dict[str, Any]] = []
    for name in DEFAULT_BUILTIN_LOCAL_TOOLS:
        entry = _BUILTIN_LOCAL_TOOL_CATALOG.get(name)
        if entry is None:
            logger.warning("DEFAULT_BUILTIN_LOCAL_TOOLS: unknown tool name {!r}, skipped", name)
            continue
        specs.append(dict(entry))
    return specs


def _get_mcp_registry():
    from dataagent.actions.tools.mcp import mcp_registry

    return mcp_registry


def _get_a2a_registry():
    from dataagent.actions.tools.a2a import a2a_registry

    return a2a_registry


def _get_local_tool_wrapper():
    from dataagent.actions.tools.local import LocalToolWrapper

    return LocalToolWrapper


def call_sync_with_event_loop(func, /, **kwargs):
    """Run a sync callable in a worker thread with a per-thread event loop."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return func(**kwargs)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


class ToolManager:
    """工具管理器，每个 Agent 实例拥有独立的 ToolManager"""

    def __init__(self, config_manager: Any | None = None):
        """Initialize a per-Agent ToolManager.

        Args:
            config_manager: Per-Agent :class:`~dataagent.config.config_manager.ConfigManager`.
                When set, local tools that declare ``_tool_context`` receive a
                :class:`~dataagent.actions.tools.context.ToolExecutionContext` with this instance.
                Same object reference as ``AgentEnv.config_manager`` / ``Runtime.config_manager``.
        """
        self.config_manager = config_manager
        self._tool_instances: dict[str, BaseTool] = {}
        self._tool_schemas: dict[str, ToolSchema] = {}
        # skills 元数据
        self._builtin_skills: dict[str, dict[str, Any]] = {}
        self._user_skills: dict[str, dict[str, Any]] = {}

        # 每个 ToolManager 拥有独立的 ToolRegistry
        self.tool_registry = ToolRegistry()
        self._mcp_registry = None
        self._a2a_registry = None

        # Per-Agent: only servers/agents registered by this ToolManager's YAML config
        self._registered_mcp_servers: set[str] = set()
        self._registered_a2a_agents: set[str] = set()

        # 懒加载缓存 - 避免重复尝试发现
        self._discovery_cache: dict[str, bool] = {}

        # 自动发现状态
        self._auto_discover_enabled = False

        # MCP server_id / A2A agent_id -> resolved hook callables (see load_tool_hooks_from_config)
        self._mcp_server_hooks: dict[str, Any] = {}
        self._a2a_agent_hooks: dict[str, Any] = {}

    @property
    def mcp_registry(self):
        """获取MCP工具注册表（延迟加载）"""
        if self._mcp_registry is None:
            self._mcp_registry = _get_mcp_registry()
        return self._mcp_registry

    @property
    def a2a_registry(self):
        """获取A2A工具注册表（延迟加载）"""
        if self._a2a_registry is None:
            self._a2a_registry = _get_a2a_registry()
        return self._a2a_registry

    @staticmethod
    def workspace_allow_path_list(config: Mapping[str, Any]) -> list[str]:
        """Parse ``WORKSPACE.allow_path`` — absolute path list, read-only like skill roots."""
        workspace_cfg = config.get("WORKSPACE")
        if not isinstance(workspace_cfg, Mapping):
            return []
        raw = workspace_cfg.get("allow_path")
        if raw is None:
            return []
        if isinstance(raw, (str, bytes)):
            raise ValueError("WORKSPACE.allow_path must be a list of absolute path strings, not a single string.")
        if not isinstance(raw, Sequence):
            raise ValueError("WORKSPACE.allow_path must be a list of absolute path strings.")
        out: list[str] = []
        for item in raw:
            s = str(item).strip()
            if not s:
                continue
            out.append(s)
        return out

    @staticmethod
    def extract_skill_allowlist(
        tools_config: Mapping[str, Any] | None,
        source: str,
    ) -> set[str]:
        """Return allowlist for builtin/user skills, defaulting to allow-none."""
        if not isinstance(tools_config, Mapping):
            return set()
        skills_config = tools_config.get("skills")
        if skills_config is None:
            return set()
        if not isinstance(skills_config, Mapping):
            raise ValueError("TOOLS.skills must be a mapping with optional builtin/user allowlists.")
        if "user" in skills_config:
            raise ValueError(
                "TOOLS.skills.user is no longer supported. "
                "User skills are auto-discovered from the user skills directory at prompt-build time."
            )
        if source not in skills_config:
            return set()
        configured = skills_config.get(source)
        if configured is None:
            return set()
        if not isinstance(configured, Sequence) or isinstance(configured, (str, bytes)):
            raise ValueError(f"TOOLS.skills.{source} must be a list of skill names.")
        return {str(item).strip() for item in configured if str(item).strip()}

    @staticmethod
    def extract_skill_directory_paths(tools_config: Mapping[str, Any] | None) -> list[str]:
        """Return builtin skill directory roots from TOOLS.skills.custom_dirs.

        The YAML form is a list of one or more directories. Relative paths are
        resolved under the installed dataagent package unless the caller already
        provides absolute paths.
        """
        if not isinstance(tools_config, Mapping):
            return []
        skills_config = tools_config.get("skills")
        if skills_config is None:
            return ["actions/skills"]
        if not isinstance(skills_config, Mapping):
            raise ValueError("TOOLS.skills must be a mapping.")
        raw = skills_config.get("custom_dirs")
        if raw is None:
            return ["actions/skills"]
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ValueError("TOOLS.skills.custom_dirs must be a list of directory paths.")
        paths = [str(item).strip() for item in raw if str(item).strip()]
        return paths or ["actions/skills"]

    @staticmethod
    def discover_skills_from_root(
        *,
        root: Path,
        allowlist: set[str] | None,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """Discover skills from a root directory and apply allowlist filtering."""
        if not root.is_dir():
            return [], set()

        discovered: list[dict[str, Any]] = []
        discovered_names: set[str] = set()
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            metadata = ToolManager._parse_skill_frontmatter(child)
            if metadata is None:
                continue
            discovered_names.add(metadata["name"])
            if allowlist is not None and metadata["name"] not in allowlist:
                continue
            discovered.append(metadata)
        return discovered, discovered_names

    @staticmethod
    def is_explicit_sub_agent_tool_entry(tool_config: Mapping[str, Any]) -> bool:
        """Return whether a ``TOOLS.local_functions`` entry declares ``sub_agent_tool``."""
        fn = tool_config.get("function") or tool_config.get("name")
        return str(fn or "").strip() == "sub_agent_tool"

    @staticmethod
    def resolve_subagent_config_path(raw_path: Any) -> Path:
        """Resolve and validate one ``SUBAGENT_CONFIGS`` entry path (absolute only)."""
        from dataagent.core.agents.subagent_config import resolve_subagent_config_path as _resolve

        return _resolve(raw_path)

    @staticmethod
    def load_subagent_catalog_metadata(path: Path) -> tuple[str, str]:
        """Load ``AGENT_CONFIG.name`` and ``description`` from a subagent yaml file."""
        from dataagent.core.agents.subagent_config import load_subagent_catalog_metadata as _load

        return _load(path)

    @staticmethod
    def _generate_schema(tool: BaseTool) -> ToolSchema:
        """生成工具Schema"""
        return tool.get_schema()

    @staticmethod
    def _extract_skill_allowlist(
        tools_config: Mapping[str, Any] | None,
        source: str,
    ) -> set[str]:
        """Backward-compatible alias for :meth:`extract_skill_allowlist`."""
        return ToolManager.extract_skill_allowlist(tools_config, source)

    @staticmethod
    def _extract_skill_directory_paths(tools_config: Mapping[str, Any] | None) -> list[str]:
        """Backward-compatible alias for :meth:`extract_skill_directory_paths`."""
        return ToolManager.extract_skill_directory_paths(tools_config)

    @staticmethod
    def _parse_skill_frontmatter(skill_root: Path) -> dict[str, Any] | None:
        """Parse SKILL.md frontmatter into normalized skill metadata."""
        skill_md = skill_root / "SKILL.md"
        if not skill_md.is_file():
            return None

        content = skill_md.read_text(encoding="utf-8")
        if not content.startswith("---\n"):
            logger.warning(f"Skipping skill without YAML frontmatter: {skill_md}")
            return None

        delimiter = "\n---\n"
        end_idx = content.find(delimiter, 4)
        if end_idx == -1:
            logger.warning(f"Skipping skill with unterminated frontmatter: {skill_md}")
            return None

        frontmatter = content[4:end_idx]
        try:
            parsed = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError as exc:
            logger.warning(f"Skipping skill with invalid frontmatter: {skill_md} ({exc})")
            return None
        if not isinstance(parsed, Mapping):
            logger.warning(f"Skipping skill with non-mapping frontmatter: {skill_md}")
            return None

        name = str(parsed.get("name") or "").strip()
        description = str(parsed.get("description") or "").strip()
        if not name or not description:
            logger.warning(f"Skipping skill missing required name/description: {skill_md}")
            return None

        return {
            "name": name,
            "path": str(skill_root.resolve()),
            "description": description,
        }

    @staticmethod
    def _resolve_local_tool_description_from_config(tool_config: dict[str, Any]) -> str | None:
        """Resolve tool description from a YAML ``local_functions`` entry.

        Returns:
            Explicit description when the config key is present.
            ``None`` when the key is absent so registration falls back to the function docstring.
        """
        if "description" not in tool_config:
            return None
        raw = tool_config.get("description")
        if raw is None:
            return ""
        return str(raw)

    @staticmethod
    def _merge_sub_agent_yaml_supplement_into_docstring(base_doc: str, yaml_supplement: str) -> str:
        """Append YAML ``description`` to ``sub_agent_tool`` docstring before the Args section.

        The base function docstring is preserved; YAML text is added as deployment-specific
        guidance for the parent agent configuration.

        Args:
            base_doc: Original ``sub_agent_tool`` docstring from source code.
            yaml_supplement: ``TOOLS.local_functions[].description`` for ``sub_agent_tool``.

        Returns:
            Merged docstring used for LLM tool binding.
        """
        base = (base_doc or "").rstrip()
        supplement = str(yaml_supplement or "").strip()
        if not supplement:
            return base
        indented = "\n".join(f"    {line}" if line else "" for line in supplement.splitlines())
        block = f"\n\n    Supplement (from agent configuration):\n\n{indented}\n"
        args_markers = ("\n    Args:", "\n    Args\n", "\n    Parameters:", "\n    Parameters\n")
        for marker in args_markers:
            idx = base.find(marker)
            if idx != -1:
                return f"{base[:idx]}{block}{base[idx:]}"
        return f"{base}{block}"

    @staticmethod
    def _is_explicit_sub_agent_tool_entry(tool_config: Mapping[str, Any]) -> bool:
        """Backward-compatible alias for :meth:`is_explicit_sub_agent_tool_entry`."""
        return ToolManager.is_explicit_sub_agent_tool_entry(tool_config)

    @staticmethod
    def _resolve_subagent_config_path(raw_path: Any) -> Path:
        """Backward-compatible alias for :meth:`resolve_subagent_config_path`."""
        return ToolManager.resolve_subagent_config_path(raw_path)

    @staticmethod
    def _load_subagent_catalog_metadata(path: Path) -> tuple[str, str]:
        """Backward-compatible alias for :meth:`load_subagent_catalog_metadata`."""
        return ToolManager.load_subagent_catalog_metadata(path)

    @staticmethod
    def _resolve_registered_tool_description(
        *,
        tool_name: str,
        func: Any,
        yaml_description: str | None,
    ) -> str | None:
        """Build the tool description passed to ``register_local_tool``.

        Only ``sub_agent_tool`` consumes YAML ``description`` (appended before ``Args:``).
        All other local tools ignore YAML ``description`` and use ``func.__doc__`` only.

        Args:
            tool_name: Registered tool name (function name unless overridden).
            func: Imported callable being registered.
            yaml_description: YAML ``description`` when the key is present, else ``None``.

        Returns:
            Explicit description for registration, or ``None`` to fall back to ``func.__doc__``.
        """
        if tool_name != "sub_agent_tool" or yaml_description is None:
            return None
        return ToolManager._merge_sub_agent_yaml_supplement_into_docstring(
            func.__doc__ or "",
            yaml_description,
        )

    @staticmethod
    def _load_hooks_from_tool_config(entry: dict[str, Any]):
        """Parse ``hooks`` from a TOOLS registry entry (local / mcp_servers / A2A)."""
        from dataagent.actions.tools.hooks.config import load_tool_hooks_from_config

        return load_tool_hooks_from_config(entry.get("hooks"))

    @staticmethod
    def _merge_job_tool_supplement_into_docstring(base_doc: str, supplement: str) -> str:
        """Append job-tool catalog text before the Args section when present."""
        doc = (base_doc or "").strip()
        supplement = (supplement or "").strip()
        if not supplement:
            return doc
        if "Args:" in doc:
            head, tail = doc.split("Args:", 1)
            merged = f"{head.rstrip()}\n\n{supplement}\n\nArgs:{tail}"
            return merged.strip()
        return f"{doc}\n\n{supplement}".strip() if doc else supplement

    @classmethod
    def _build_sub_agent_tool_yaml_supplement(cls, config: Mapping[str, Any]) -> str:
        """Build dynamic + static supplement for implicit ``sub_agent_tool`` registration."""
        entries = config.get("SUBAGENT_CONFIGS") or []
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            raise ValueError("SUBAGENT_CONFIGS must be a list of mappings with 'path'")
        catalog_lines: list[str] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("SUBAGENT_CONFIGS items must be mappings with 'path'")
            path = cls.resolve_subagent_config_path(entry.get("path"))
            _name, description = cls.load_subagent_catalog_metadata(path)
            catalog_lines.append(f"- {path}: {description}")
        blocks = []
        if catalog_lines:
            blocks.append(SUBAGENT_TOOL_CATALOG_HEADER)
            blocks.extend(catalog_lines)
            blocks.append("")
        blocks.append(SUBAGENT_TOOL_FIXED_CALL_INSTRUCTIONS.strip())
        return "\n".join(blocks).strip()

    @classmethod
    def _build_job_subagent_tool_supplement(cls, config: Mapping[str, Any]) -> str:
        """Build dynamic catalog supplement for implicit job lifecycle tools."""
        from dataagent.core.agents.registry import resolve_agent_id_from_yaml

        entries = config.get("SUBAGENT_CONFIGS") or []
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            raise ValueError("SUBAGENT_CONFIGS must be a list of mappings with 'path'")
        catalog_lines: list[str] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("SUBAGENT_CONFIGS items must be mappings with 'path'")
            path = cls.resolve_subagent_config_path(entry.get("path"))
            with open(path, encoding="utf-8") as handle:
                payload = yaml.safe_load(handle) or {}
            if not isinstance(payload, Mapping):
                raise ValueError(f"SUBAGENT_CONFIGS yaml root must be a mapping: {path}")
            agent_id = resolve_agent_id_from_yaml(path, payload)
            _name, description = cls.load_subagent_catalog_metadata(path)
            catalog_lines.append(f"- {agent_id}: {description}")
        blocks = []
        if catalog_lines:
            blocks.append(JOB_SUBAGENT_TOOL_CATALOG_HEADER)
            blocks.extend(catalog_lines)
            blocks.append("")
        blocks.append(JOB_SUBAGENT_TOOL_FIXED_CALL_INSTRUCTIONS.strip())
        return "\n".join(blocks).strip()

    def enable_auto_discover(self):
        """启用自动发现功能（只执行一次）"""
        if not self._auto_discover_enabled:
            self._auto_discover_enabled = True
            self._discover_all_sync()

    def is_auto_discover_enabled(self) -> bool:
        """检查是否已启用自动发现"""
        return self._auto_discover_enabled

    def register_local_tool(
        self,
        func_or_class,
        name: str | None = None,
        category: str = "general",
        description: str | None = None,
        **kwargs,
    ) -> Callable:
        """注册本地工具。

        Args:
            func_or_class: 可调用函数或 ``BaseTool`` 子类。
            name: 工具名；默认取函数名。
            category: 工具分类。
            description: 工具描述；``None`` 时使用函数 docstring。
            **kwargs: 透传给工具实例的额外配置。
        """
        tool_name = name or getattr(func_or_class, "__name__", None)
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("Local tool name must be a non-empty string.")
        if callable(func_or_class) and not inspect.isclass(func_or_class):
            LocalToolWrapper = _get_local_tool_wrapper()
            tool_context = self._build_tool_execution_context()
            tool_wrapper = LocalToolWrapper(
                func_or_class,
                tool_name,
                category,
                description,
                tool_context=tool_context,
                **kwargs,
            )
        elif inspect.isclass(func_or_class) and issubclass(func_or_class, BaseTool):
            tool_wrapper = func_or_class(name=tool_name, category=category, description=description, **kwargs)
        else:
            raise ValueError(f"Unsupported tool type: {type(func_or_class)}")
        self.tool_registry.register(tool_name, tool_wrapper)
        self._tool_instances[tool_name] = tool_wrapper
        schema = self._generate_schema(tool_wrapper)
        self._tool_schemas[tool_name] = schema
        return func_or_class

    def register_mcp_server(
        self,
        server_id: str,
        transport_type: str,
        config: dict[str, Any],
        category: str = "general",
        description: str = "",
    ):
        """注册MCP服务器"""
        self._registered_mcp_servers.add(server_id)
        return self.mcp_registry.register_server(server_id, transport_type, config, category, description)

    def register_a2a_agent(
        self,
        agent_id: str,
        base_url: str,
        auth_token: str | None = None,
        timeout: int = 30,
        category: str = "a2a",
        description: str = "",
    ):
        """注册A2A代理"""
        self._registered_a2a_agents.add(agent_id)
        return self.a2a_registry.register_agent(agent_id, base_url, auth_token, timeout, category, description)

    async def discover_mcp_tools(self, server_id: str) -> list[str]:
        """发现并注册MCP工具（per-Agent）"""
        self._registered_mcp_servers.add(server_id)
        tools = await self.mcp_registry.list_server_tools(server_id)
        hook_lists = self._mcp_server_hooks.get(server_id)
        for tool in tools:
            if hook_lists is not None:
                from dataagent.actions.tools.hooks.config import attach_hooks_to_tool

                attach_hooks_to_tool(tool, hook_lists)
            self._tool_instances[tool.name] = tool
            schema = tool.get_schema()
            self._tool_schemas[tool.name] = schema
        return [tool.name for tool in tools]

    async def discover_a2a_tools(self, agent_id: str) -> list[str]:
        """发现并注册A2A工具（per-Agent）"""
        self._registered_a2a_agents.add(agent_id)
        tools = await self.a2a_registry.list_agent_tools(agent_id)
        hook_lists = self._a2a_agent_hooks.get(agent_id)
        for tool in tools:
            if hook_lists is not None:
                from dataagent.actions.tools.hooks.config import attach_hooks_to_tool

                attach_hooks_to_tool(tool, hook_lists)
            self._tool_instances[tool.name] = tool
            schema = tool.get_schema()
            self._tool_schemas[tool.name] = schema
        return [tool.name for tool in tools]

    def init_from_config(self, config: dict[str, Any]):
        """从配置字典初始化工具

        Args:
            config: 包含TOOLS配置和其他配置的字典，支持以下字段：
                - TOOLS: 工具配置字典
                - AGENT_CONFIG.enable_human_feedback: 是否启用人工反馈工具
                - enable_human_feedback: 是否启用人工反馈工具（直接配置）
        """
        logger.trace("=== Initializing Tool Manager 🛠️ ===")

        tools_config = config.get("TOOLS", {})
        self._builtin_skills = {skill["name"]: skill for skill in self._discover_builtin_skills(config)}
        self._user_skills = {}

        # 检查是否启用 HITL 功能
        enable_hitl = config.get("enable_human_feedback", False)
        if not enable_hitl:
            # 从 AGENT_CONFIG 中读取
            agent_config = config.get("AGENT_CONFIG", {})
            enable_hitl = agent_config.get("enable_human_feedback", False)

        if enable_hitl:
            self._register_hitl_tool()

        self._register_builtin_local_tools(tools_config)
        self._register_implicit_job_tools(config)

        if not tools_config:
            return
        if "local_functions" in tools_config:
            self._register_local_tools(tools_config["local_functions"])
        if "mcp_servers" in tools_config:
            self._register_mcp_servers_from_config(tools_config["mcp_servers"])
        if "A2A" in tools_config:
            self._register_a2a_tools_from_config(tools_config["A2A"])

        # 对已注册的 MCP / A2A 做全量工具发现
        if self._registered_mcp_servers or self._registered_a2a_agents:
            self.enable_auto_discover()

    def get(self, name: str) -> BaseTool:
        """获取工具实例（支持A2A和MCP懒加载）"""
        if name in self._tool_instances:
            return self._tool_instances[name]

        # 尝试懒加载A2A工具
        if self._try_lazy_discover_a2a(name) and name in self._tool_instances:
            return self._tool_instances[name]

        # 尝试懒加载MCP工具
        if self._try_lazy_discover_mcp(name) and name in self._tool_instances:
            return self._tool_instances[name]

        raise ToolError(f"Tool '{name}' not found")

    def exists(self, name: str) -> bool:
        """检查工具是否存在"""
        return name in self._tool_instances

    def call(self, name: str, **kwargs) -> ToolResult:
        """调用工具（同步版本）"""
        tool = self.get(name)
        return tool.call(**kwargs)

    async def aget(self, name: str) -> BaseTool:
        """异步获取工具实例（支持A2A和MCP懒加载）"""
        if name in self._tool_instances:
            return self._tool_instances[name]

        # 尝试懒加载A2A工具
        if await self._try_lazy_discover_a2a_async(name) and name in self._tool_instances:
            return self._tool_instances[name]

        # 尝试懒加载MCP工具
        if await self._try_lazy_discover_mcp_async(name) and name in self._tool_instances:
            return self._tool_instances[name]

        raise ToolError(f"Tool '{name}' not found")

    async def acall(self, name: str, **kwargs) -> ToolResult:
        """调用工具（异步版本，支持懒加载）"""
        tool = await self.aget(name)  # 使用异步获取方法
        acall = getattr(tool, "acall", None)
        if callable(acall):
            result = acall(**kwargs)
            if inspect.isawaitable(result):
                result = await result
        else:
            # 对于仅提供同步接口但内部可能依赖 asyncio.get_event_loop() 的工具，
            # 在线程中显式挂载一个事件循环，避免 "There is no current event loop"。
            result = await asyncio.to_thread(call_sync_with_event_loop, tool.call, **kwargs)

        # 当工具返回失败结果时，抛出 ToolError 以触发调用方的重试逻辑
        if isinstance(result, ToolResult) and not result.success:
            from dataagent.core.managers.action_manager.base import ErrorType

            raise ToolError(
                message=result.error or "Tool execution failed",
                error_type=result.error_type or ErrorType.UNKNOWN,
                retriable=result.retriable,
                max_retries=result.max_retries,
            )
        return result

    def list_tools(self, category: str | None = None, tool_type: ToolType | None = None) -> list[str]:
        """列出工具名称"""
        tools = self._tool_instances.items()

        if category is not None:
            tools = [(name, tool) for name, tool in tools if tool.category == category]

        if tool_type is not None:
            tools = [(name, tool) for name, tool in tools if tool.tool_type == tool_type]

        return [name for name, tool in tools]

    def get_all_tool_instances(self) -> list[BaseTool]:
        """Return all registered tool instances (for Runtime.get_tools_for_llm)."""
        return list(self._tool_instances.values())

    def list_skills(self) -> list[dict[str, Any]]:
        """列出已配置的skills元数据"""
        return [*self._builtin_skills.values(), *self._user_skills.values()]

    def list_builtin_skills(self) -> list[dict[str, Any]]:
        """列出 builtin skills 元数据。"""
        return list(self._builtin_skills.values())

    def list_user_skills(self) -> list[dict[str, Any]]:
        """列出 user skills 元数据。"""
        return list(self._user_skills.values())

    def get_skill(self, name: str) -> dict[str, Any] | None:
        """根据名称获取skill元数据"""
        return self._builtin_skills.get(name) or self._user_skills.get(name)

    def refresh_user_skills(
        self,
        *,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """重扫用户 skills 目录，并刷新用户 skill 元数据与 alias。"""
        if not user_id:
            self._user_skills = {}
            return []

        user_skills, _ = self.discover_skills_from_root(
            root=resolve_user_root(user_id=user_id) / "skills",
            allowlist=None,
        )
        active_user_skills: dict[str, dict[str, Any]] = {}
        for skill in user_skills:
            if skill["name"] in self._builtin_skills:
                kept_path = self._builtin_skills[skill["name"]]["path"]
                logger.warning(
                    f"User skill '{skill['name']}' is ignored because a builtin skill with the same name exists. "
                    f"Builtin path: {kept_path}. Ignored user path: {skill['path']}."
                )
                continue
            if skill["name"] in active_user_skills:
                kept_path = active_user_skills[skill["name"]]["path"]
                logger.warning(
                    f"Duplicate user skill '{skill['name']}' detected. "
                    f"Keeping '{kept_path}' and ignoring '{skill['path']}' (first-win)."
                )
                continue
            active_user_skills[skill["name"]] = skill

        self._user_skills = active_user_skills
        for skill in self.list_skills():
            logger.trace(f"✅ Skill: '{skill['name']}' registered with path: {skill['path']}.")
        return self.list_user_skills()

    def get_schema(self, name: str) -> ToolSchema:
        """获取工具Schema"""
        if name not in self._tool_schemas:
            raise ToolError(f"Schema for tool '{name}' not found")
        return self._tool_schemas[name]

    def get_langchain_tool(self, name: str):
        """获取LangChain兼容的工具"""
        tool = self.get(name)
        return tool.to_langchain_tool()

    def get_tools_for_llm(self, tool_names: list[str]) -> list[dict[str, Any]]:
        """获取用于LLM function calling的工具定义"""
        tools = []
        for name in tool_names:
            if self.exists(name):
                schema = self.get_schema(name)
                tools.append(schema.to_openai_function())
        return tools

    def get_tools_by_type(self, tool_type: ToolType) -> dict[str, BaseTool]:
        """按类型获取工具"""
        return {name: tool for name, tool in self._tool_instances.items() if tool.tool_type == tool_type}

    def get_tool_info(self, name: str) -> dict[str, Any]:
        """获取工具详细信息"""
        if name not in self._tool_instances:
            raise ToolError(f"Tool '{name}' not found")

        tool = self._tool_instances[name]
        schema = self._tool_schemas[name]

        metadata_schema = schema.to_metadata()
        metadata_schema_with_name = {"name": tool.name, **metadata_schema}

        return {
            "name": tool.name,
            "type": tool.tool_type.value,
            "category": tool.category,
            "description": tool.description,
            "schema": metadata_schema_with_name,
            "config": getattr(tool, "config", {}),
        }

    def list_tool_categories(self) -> list[str]:
        """列出所有工具分类"""
        categories = set()
        for tool in self._tool_instances.values():
            categories.add(tool.category)
        return sorted(categories)

    def get_tools_summary(self) -> dict[str, Any]:
        """获取工具总览信息"""
        total_tools = len(self._tool_instances)
        by_type = {}
        by_category = {}

        for tool in self._tool_instances.values():
            # 按类型统计
            tool_type = tool.tool_type.value
            by_type[tool_type] = by_type.get(tool_type, 0) + 1

            # 按分类统计
            category = tool.category
            by_category[category] = by_category.get(category, 0) + 1

        return {
            "total_tools": total_tools,
            "by_type": by_type,
            "by_category": by_category,
            "available_categories": sorted(by_category.keys()),
        }

    async def health_check(self) -> dict[str, Any]:
        """检查所有工具的健康状态"""
        mcp_servers = {}
        for server_id in self.mcp_registry.list_servers():
            mcp_servers[server_id] = await self.mcp_registry.ping_server(server_id)
        health_status = {
            "mcp_servers": mcp_servers,
            "a2a_agents": await self.a2a_registry.health_check(),
            "local_tools": len([t for t in self._tool_instances.values() if t.tool_type == ToolType.LOCAL_FUNCTION]),
            "total_tools": len(self._tool_instances),
        }

        return health_status

    async def cleanup(self):
        """清理本 Agent 自己的工具实例（不触及全局 MCP/A2A 连接）。"""
        logger.debug("🧹 清理 per-Agent 资源...")

        # 清理 local tools（ToolRegistry + 内部缓存）
        self._tool_instances.clear()
        self._tool_schemas.clear()
        self._discovery_cache.clear()
        self._builtin_skills.clear()
        self._user_skills.clear()
        self._registered_mcp_servers.clear()
        self._registered_a2a_agents.clear()
        self._mcp_server_hooks.clear()
        self._a2a_agent_hooks.clear()

        if self.tool_registry:
            self.tool_registry.clear()

        # 不清理全局 mcp_registry / a2a_registry — 它们是跨 Agent 共享的连接层
        logger.debug("   ✅ per-Agent 工具管理器资源清理完成")

    def _build_tool_execution_context(self):
        """Build :class:`~dataagent.actions.tools.context.ToolExecutionContext` for local tool execution."""
        from dataagent.actions.tools.context import ToolExecutionContext

        return ToolExecutionContext(config_manager=self.config_manager)

    async def _try_lazy_discover_a2a_async(self, name: str) -> bool:
        """异步尝试懒加载A2A工具（仅限本 Agent 注册的 agent）"""
        if "." not in name:
            return False

        agent_id = name.split(".")[0]

        # 检查是否已经尝试过
        if agent_id in self._discovery_cache:
            return False

        # 仅允许本 Agent 注册过的 A2A agent（防止跨 Agent 泄露）
        if agent_id not in self._registered_a2a_agents:
            return False

        # 标记为已尝试
        self._discovery_cache[agent_id] = True

        try:
            await self.discover_a2a_tools(agent_id)
            return name in self._tool_instances
        except Exception:
            return False

    def _register_builtin_local_tools(self, tools_config: Mapping[str, Any] | None) -> None:
        """Register builtin local tools: constants catalog ∩ DEFAULT_BUILTIN_LOCAL_TOOLS; YAML may override."""
        specs = _builtin_local_tool_specs_from_constants()
        if isinstance(tools_config, Mapping) and "builtin" in tools_config:
            raw = tools_config.get("builtin")
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                specs = list(raw)
        self._register_local_tools(specs)

    def _register_hitl_tool(self):
        """注册 HITL 工具（内部方法）"""
        try:
            from dataagent.actions.tools.local_tool.tools import request_human_feedback

            self.register_local_tool(
                request_human_feedback,
                name="request_human_feedback",
                category="system",
                description="Request human feedback for confirmation",
            )
            logger.trace("✅ HITL tool 'request_human_feedback' registered.")
        except Exception as e:
            logger.warning(f"❌ Failed to register HITL tool: {e}")

    def _register_local_tools(self, tools: list[dict[str, Any]]):
        """Register local function tools from YAML ``TOOLS.local_functions`` entries."""
        for tool_config in tools:
            if not isinstance(tool_config, dict):
                continue
            if self._is_explicit_sub_agent_tool_entry(tool_config):
                raise ValueError(
                    "TOOLS.local_functions must not declare sub_agent_tool; "
                    "use SUBAGENT_CONFIGS to register subagents instead."
                )
            module_path = tool_config.get("module")
            name = tool_config.get("function") or tool_config.get("name")
            category = tool_config.get("category", "general")
            config = tool_config.get("config", {})
            if not isinstance(config, dict):
                config = {}
            if not module_path or not name:
                continue
            try:
                import importlib

                module = importlib.import_module(module_path)
                func = getattr(module, name)
                register_kwargs: dict[str, Any] = {"name": name, "category": category, **config}
                yaml_description = self._resolve_local_tool_description_from_config(tool_config)
                resolved_description = self._resolve_registered_tool_description(
                    tool_name=str(name),
                    func=func,
                    yaml_description=yaml_description,
                )
                if resolved_description is not None:
                    register_kwargs["description"] = resolved_description
                self.register_local_tool(func, **register_kwargs)
                hook_lists = self._load_hooks_from_tool_config(tool_config)
                if hook_lists.pre or hook_lists.post:
                    from dataagent.actions.tools.hooks.config import attach_hooks_to_tool

                    attach_hooks_to_tool(self._tool_instances[name], hook_lists)
                logger.trace(f"✅ Local tool: '{name}' registered.")
            except Exception as e:
                logger.warning(f"❌ Local tool: '{name}' registration failed: {e}.")

    def _register_implicit_sub_agent_tool(self, config: Mapping[str, Any]) -> None:
        """Register ``sub_agent_tool`` when ``SUBAGENT_CONFIGS`` is non-empty."""
        entries = config.get("SUBAGENT_CONFIGS") or []
        if not entries:
            return
        from dataagent.actions.tools.local_tool.tools import sub_agent_tool

        supplement = self._build_sub_agent_tool_yaml_supplement(config)
        description = self._merge_sub_agent_yaml_supplement_into_docstring(sub_agent_tool.__doc__ or "", supplement)
        try:
            self.register_local_tool(sub_agent_tool, name="sub_agent_tool", description=description)
            logger.trace("✅ Implicit sub_agent_tool registered from SUBAGENT_CONFIGS.")
        except Exception as e:
            logger.warning("❌ Implicit sub_agent_tool registration failed: {}", e)

    def _register_implicit_job_tools(self, config: Mapping[str, Any]) -> None:
        """Register subagent job lifecycle tools when ``SUBAGENT_CONFIGS`` is non-empty."""
        entries = config.get("SUBAGENT_CONFIGS") or []
        if not entries:
            return
        from dataagent.actions.tools.local_tool.job_tools.cancel_subagent import cancel_subagent
        from dataagent.actions.tools.local_tool.job_tools.collect_subagent import collect_subagent
        from dataagent.actions.tools.local_tool.job_tools.poll_subagent import poll_subagent
        from dataagent.actions.tools.local_tool.job_tools.submit_subagent import submit_subagent
        from dataagent.actions.tools.local_tool.workspace_tool import inspect_workspace, search_workspaces

        supplement = self._build_job_subagent_tool_supplement(config)
        tools = [
            (submit_subagent, "submit_subagent"),
            (poll_subagent, "poll_subagent"),
            (collect_subagent, "collect_subagent"),
            (cancel_subagent, "cancel_subagent"),
            (search_workspaces, "search_workspaces"),
            (inspect_workspace, "inspect_workspace"),
        ]
        for func, name in tools:
            description = None
            if name == "submit_subagent":
                description = self._merge_job_tool_supplement_into_docstring(func.__doc__ or "", supplement)
            try:
                register_kwargs: dict[str, Any] = {"name": name, "category": "job"}
                if description is not None:
                    register_kwargs["description"] = description
                self.register_local_tool(func, **register_kwargs)
                logger.trace("✅ Implicit job tool '{}' registered from SUBAGENT_CONFIGS.", name)
            except Exception as exc:
                logger.warning("❌ Implicit job tool '{}' registration failed: {}", name, exc)

    def _register_mcp_servers_from_config(self, servers: list[dict[str, Any]]):
        """从配置注册MCP服务器"""
        for server_config in servers:
            if not isinstance(server_config, dict):
                continue
            server_id = server_config.get("server_id")
            transport_type = server_config.get("transport_type", "stdio")
            config = server_config.get("config", {})
            if not server_id:
                logger.warning("❌ MCP server missing 'server_id' in configuration")
                continue
            try:
                self.register_mcp_server(server_id=server_id, transport_type=transport_type, config=config)
                hook_lists = self._load_hooks_from_tool_config(server_config)
                if hook_lists.pre or hook_lists.post:
                    self._mcp_server_hooks[server_id] = hook_lists
                logger.trace(f"✅ MCP server: '{server_id}' registered with {transport_type} transport.")
            except Exception as e:
                logger.warning(f"❌ MCP server: '{server_id}' registration failed: {e}.")

    def _register_a2a_tools_from_config(self, tools: list[dict[str, Any]]):
        """从配置注册A2A工具"""
        for tool_config in tools:
            if not isinstance(tool_config, dict):
                continue

            # 获取代理ID（字典的第一个键）
            agent_id = list(tool_config.keys())[0]
            agent_config = tool_config[agent_id]

            base_url = agent_config.get("base_url")
            auth_token = agent_config.get("auth_token")
            timeout = agent_config.get("timeout", 30)

            if not base_url:
                logger.warning(f"❌ A2A agent: '{agent_id}' missing base_url configuration")
                continue

            try:
                self.register_a2a_agent(agent_id=agent_id, base_url=base_url, auth_token=auth_token, timeout=timeout)
                hook_lists = self._load_hooks_from_tool_config(agent_config)
                if hook_lists.pre or hook_lists.post:
                    self._a2a_agent_hooks[agent_id] = hook_lists
                logger.trace(f"✅ A2A agent: '{agent_id}' registered.")

            except Exception as e:
                logger.warning(f"❌ A2A agent: '{agent_id}' registration failed: {e}.")

    def _discover_builtin_skills(self, config: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Discover builtin skills from default and extra configured directories.

        The default `actions/skills` tree keeps its original allowlist behavior.
        Extra directories listed in `TOOLS.skills.custom_dirs` are scanned directly
        without an additional name allowlist filter.
        """
        tools_config = config.get("TOOLS", {}) if isinstance(config, Mapping) else {}
        builtin_allowlist = set(self._extract_skill_allowlist(tools_config, "builtin"))
        builtin_allowlist.update(DEFAULT_BUILTIN_SKILL_NAMES)

        discovered: list[dict[str, Any]] = []
        discovered_names: set[str] = set()

        default_root = dataagent_package_path("actions", "skills")
        default_skills, default_names = self.discover_skills_from_root(root=default_root, allowlist=builtin_allowlist)
        discovered.extend(default_skills)
        discovered_names.update(default_names)

        for rel_path in self._extract_skill_directory_paths(tools_config):
            if rel_path == "actions/skills":
                continue
            root = Path(rel_path) if Path(rel_path).is_absolute() else dataagent_package_path(*str(rel_path).split("/"))
            extra_skills, extra_names = self.discover_skills_from_root(root=root, allowlist=None)
            for skill in extra_skills:
                if skill["name"] not in discovered_names:
                    discovered.append(skill)
                    discovered_names.add(skill["name"])
            discovered_names.update(extra_names)

        return discovered

    async def _try_lazy_discover_mcp_async(self, name: str) -> bool:
        """异步尝试懒加载MCP工具（仅限本 Agent 注册的 server）"""
        if "." not in name:
            return False

        server_id = name.split(".")[0]

        # 检查是否已经尝试过
        if server_id in self._discovery_cache:
            return False

        # 仅允许本 Agent 注册过的 MCP server（防止跨 Agent 泄露）
        if server_id not in self._registered_mcp_servers:
            return False

        # 标记为已尝试
        self._discovery_cache[server_id] = True

        try:
            await self.discover_mcp_tools(server_id)
            return name in self._tool_instances
        except Exception:
            return False

    def _discover_all_sync(self):
        """同步发现本 Agent 注册的所有 MCP/A2A 工具"""
        try:
            # 仅扫描本 Agent 注册过的 MCP server
            for server_id in self._registered_mcp_servers:
                try:
                    # 同步方式运行异步发现
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # 如果事件循环正在运行，使用线程池执行
                        import concurrent.futures

                        with concurrent.futures.ThreadPoolExecutor() as executor:
                            future = executor.submit(asyncio.run, self.discover_mcp_tools(server_id))
                            tools = future.result(timeout=DEFAULT_MCP_DISCOVERY_TIMEOUT)  # 60秒超时
                            logger.trace(f"发现MCP工具 ({server_id}): {tools}")
                    else:
                        # 直接运行
                        tools = asyncio.run(self.discover_mcp_tools(server_id))
                        logger.trace(f"发现MCP工具 ({server_id}): {tools}")
                except Exception as e:
                    logger.error(f"发现MCP服务器 {server_id} 的工具时出错: {e}")

            # 仅扫描本 Agent 注册过的 A2A agent
            for agent_id in self._registered_a2a_agents:
                try:
                    # 同步方式运行异步发现
                    try:
                        loop = asyncio.get_running_loop()
                        # 如果事件循环正在运行，使用线程池执行
                        import concurrent.futures

                        with concurrent.futures.ThreadPoolExecutor() as executor:
                            future = executor.submit(asyncio.run, self.discover_a2a_tools(agent_id))
                            tools = future.result(timeout=DEFAULT_MCP_DISCOVERY_TIMEOUT)  # 60秒超时
                            logger.trace(f"发现A2A工具 ({agent_id}): {tools}")
                    except RuntimeError:
                        # 没有运行的事件循环，直接运行
                        tools = asyncio.run(self.discover_a2a_tools(agent_id))
                        logger.trace(f"发现A2A工具 ({agent_id}): {tools}")
                except Exception as e:
                    logger.error(f"发现A2A代理 {agent_id} 的工具时出错: {e}")

        except Exception as e:
            logger.error(f"初始化时发现工具失败: {e}")

    def _try_lazy_discover_a2a(self, name: str) -> bool:
        """尝试懒加载A2A工具（仅限本 Agent 注册的 agent）"""
        if "." not in name:
            return False

        agent_id = name.split(".")[0]

        # 检查是否已经尝试过
        if agent_id in self._discovery_cache:
            return False

        # 仅允许本 Agent 注册过的 A2A agent（防止跨 Agent 泄露）
        if agent_id not in self._registered_a2a_agents:
            return False

        # 标记为已尝试
        self._discovery_cache[agent_id] = True

        try:
            # 同步方式运行异步发现
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, self.discover_a2a_tools(agent_id))
                    future.result()
            else:
                asyncio.run(self.discover_a2a_tools(agent_id))
            return name in self._tool_instances
        except Exception:
            return False

    def _try_lazy_discover_mcp(self, name: str) -> bool:
        """尝试懒加载MCP工具（仅限本 Agent 注册的 server）"""
        if "." not in name:
            return False

        server_id = name.split(".")[0]

        # 检查是否已经尝试过
        if server_id in self._discovery_cache:
            return False

        # 仅允许本 Agent 注册过的 MCP server（防止跨 Agent 泄露）
        if server_id not in self._registered_mcp_servers:
            return False

        # 标记为已尝试
        self._discovery_cache[server_id] = True

        try:
            # 同步方式运行异步发现
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, self.discover_mcp_tools(server_id))
                    future.result()
            else:
                asyncio.run(self.discover_mcp_tools(server_id))
            return name in self._tool_instances
        except Exception:
            return False
