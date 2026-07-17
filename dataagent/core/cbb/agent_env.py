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
"""Agent-level Env dataclass (galatea-style).

This is distinct from ``dataagent.actions.environment.Env`` which is a tool-registry
class based on the ``@tool`` decorator.  This dataclass carries all static
configuration an Agent needs at run time: model configs, tool paths, skills,
module-mounting declarations, hooks, and iteration limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dataagent.config.config_manager import ConfigManager
    from dataagent.core.managers.action_manager.manager import ToolManager


@dataclass
class Env:
    """Static configuration passed to a galatea-style BaseAgent."""

    # flex：键为节点或模型槽位名；值为仅含调用参数的扁平 dict（无 YAML 元数据），见 flex_runtime_from_config
    llm_configs: dict[str, dict[str, Any]]
    tavily_configs: dict[str, Any]
    modules: dict[str, list[str]]
    hooks: dict[str, Any]
    # None：不限制 Actor 循环；仅在 YAML 中显式给出 AGENT_CONFIG.max_iter 数值时生效。
    # 同时用于推导图引擎 recursion_limit：None→DEFAULT_WORKFLOW_RECURSION_LIMIT，
    # 有值→max(DEFAULT, max_iter*MAX_ITER_TO_RECURSION_FACTOR)（见 dataagent.utils.constants）
    max_iter: int | None = None
    # 可选：累计 usage token 上限（YAML AGENT_CONFIG.token_limit）
    token_limit: int | None = None
    # workspace 与 hierarchy：可由 YAML config 预设，也可在运行时由 state 动态覆盖
    workspace_dir: Path | None = field(default=None)
    hierarchy: str = "MAIN"
    # 场景级指令：由 YAML SCENARIO.{mode}.instructions 写入，运行时只读
    instructions: str = ""
    # Gym 环境描述（如 SQLiteEnv.get_description()），注入 nl2sql 等 system prompt
    environment_description: str = ""
    # 工具并发数上限：None 表示不限制（使用 CPU 自动计算）；整数表示取 min(自动值, 此值)
    max_concurrency: int | None = None
    # bash 工具命令白名单：None 表示不限制；list[str] 表示仅允许列出的命令（如 ["ls", "cat", "grep"]）
    bash_tool_whitelist: list[str] | None = None
    # ── CONTEXT 压缩参数（YAML CONTEXT.* 可覆盖，None 表示使用 constants 默认值） ──
    compress_token_limit: int | None = None
    compress_message_cnt: int | None = None
    file_node_threshold: int | None = None
    # IR 替换的 recent_turns 阈值（YAML CONTEXT.recent_turns 可覆盖，None 表示用 DEFAULT_IR_RECENT_TURNS）
    ir_recent_turns: int | None = None
    # 工具结果截断长度（YAML 节点级 max_tool_result_length 可覆盖，None 表示用 DEFAULT_MAX_TOOL_RESULT_LENGTH）
    max_tool_result_length: int | None = None

    tool_manager: ToolManager | None = None
    # Per-Agent YAML configuration (not server.backend.config).
    config_manager: ConfigManager | None = None
    # Runtime governance registry built from top-level GOVERNANCE config.
    governance: Any | None = None
