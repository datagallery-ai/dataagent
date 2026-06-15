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
"""DataAgent 全局可配置常量。

本文件集中管理所有跨模块使用的可配置常量，按功能分类排列：
  - 压缩与LLM调用：消息压缩阈值、LLM 重试退避、Flex Pruner
  - 工具执行与并发控制：工具超时、文件限制、结果截断、沙箱、MCP 发现、并发度、Swarm Worker metadata / messages 简易裁剪
  - NL2SQL与IR与知识图谱：NL2SQL 各阶段参数、IR 转换/消费者、Bioinfo Skill、内置工具注册
  - UI渲染与可视化：Rich 渲染器、Context 轨迹图
  - 数据库与运行时探测：连接探测、进程等待、CPU 采样
  - 环境默认值与内置注册：用户/会话/运行 ID 回退、TodoList

每个常量均标注了当前定义位置、建议的 YAML 配置路径，以及是否已在 YAML 中可配置。
"""

# ============================================================================
# 压缩与LLM调用
# ============================================================================


# ── Cross-Session Recall ────────────────────────────────────────────────────
# 当前定义位置: dataagent/core/flex/hooks/cross_session_recall.py
# 建议 YAML 路径: CROSS_SESSION_RECALL.enable / top_k / max_chars_per_session

DEFAULT_CROSS_SESSION_RECALL_TOP_K: int = 3
"""跨 Session 召回的历史 Session 数量上限。"""

DEFAULT_CROSS_SESSION_RECALL_MAX_CHARS: int = 1500
"""每个历史 Session 注入到 prompt 的最大字符数。"""


# ── 消息压缩 ─────────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/utils/compression_utils.py
# 建议 YAML 路径: AGENT_CONFIG.compress_token_limit

DEFAULT_COMPRESS_TOKEN_LIMIT: int = 32768
"""压缩触发 token 阈值。消息 token 数超过此值 1.2 倍时触发 LLM 折叠压缩。"""

DEFAULT_COMPRESS_MESSAGE_CNT: int = 200
"""压缩触发消息数量阈值。消息数超过此值时触发压缩。"""

DEFAULT_COMPRESS_FOLD_TEMPERATURE: float = 0.7
"""语义折叠压缩时 LLM 调用温度，平衡确定性归纳与语义保留。"""

DEFAULT_COMPRESS_MAX_RETRIES: int = 3
"""压缩操作失败时的最大重试次数。"""


# ── LLM 调用重试 ─────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/core/managers/llm_manager/llm_client.py
# YAML 可选: MODEL[*].params.num_retries（覆盖重试次数 N）

DEFAULT_LLM_MAX_RETRIES: int = 3
"""429/Timeout（litellm retry_policy）与 5xx/连接（DataAgent 薄层）的重试次数上限。"""

DEFAULT_LLM_RETRY_POLICY: dict[str, int] = {
    "BadRequestErrorRetries": 0,
    "AuthenticationErrorRetries": 0,
    "ContentPolicyViolationErrorRetries": 0,
}
"""litellm 不可重试 4xx；429/Timeout 次数由 llm_client._normalize 按 max_attempts 注入。"""


# ── Flex Pruner Hook ─────────────────────────────────────────────────────────
# 当前定义位置: dataagent/core/flex/hooks/pruner.py (复用 compression_utils 中值)
# 建议 YAML 路径: AGENT_CONFIG.pruner_token_limit

DEFAULT_PRUNER_TOKEN_LIMIT: int = DEFAULT_COMPRESS_TOKEN_LIMIT
"""Flex Pruner 在规划器 pre-hook 中触发消息压缩的 token 阈值。"""

# ============================================================================
# 工具执行与并发控制
# ============================================================================


# ── 工具调用超时 ─────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/actions/tools/local_tool/tools.py
# 建议 YAML 路径: TOOLS.*_timeout

DEFAULT_BASH_TIMEOUT: int = 600
"""Bash 工具命令执行的默认超时（秒）。"""

DEFAULT_SUBAGENT_TOOL_TIMEOUT: int = 3600
"""子 Agent 工具调用的默认超时（秒）。"""

SUBAGENT_TOOL_CATALOG_HEADER: str = "可选的 config_path 及用途："
"""``sub_agent_tool`` 工具说明中 worker 目录段标题（由 ``SUBAGENT_CONFIGS`` 动态生成列表）。"""

SUBAGENT_TOOL_FIXED_CALL_INSTRUCTIONS: str = """\
调用时请在参数中显式传入 config_path 为上述绝对路径之一，并严格遵循工具的参数要求，例如：
- query: "What is 5 + 3 * 2"
- config_path: "/abs/path/to/subagent.yaml"
"""
"""``sub_agent_tool`` 固定调用说明（硬编码，不放入 ``SUBAGENT_CONFIGS``）。"""

MAX_WORKER_METADATA_ARTIFACTS: int = 50
"""Worker ``metadata.json`` 中 ``artifacts`` 路径列表最大条数。

超出时丢弃更早的记录、保留列表末尾（可视作较新的路径）。
当前使用位置: dataagent/core/swarm/worker_metadata.py（``upsert_worker_metadata``）。
"""

WORKER_LOCK_TTL_GRACE_SECONDS: int = 60
"""子 Agent worker 锁 TTL 在 ``sub_agent_tool`` 超时之外的额外缓冲（秒）。

当前使用位置: dataagent/actions/tools/local_tool/tools.py（``acquire_worker_lock`` 的 ``ttl_seconds``）。
"""

DEFAULT_GREP_TIMEOUT: int = 30
"""Grep 子进程超时（秒）。"""

DEFAULT_GLOB_MAX_RESULTS: int = 100
"""Glob 文件搜索默认最大结果数。"""

DEFAULT_GREP_HEAD_LIMIT: int = 250
"""Grep 搜索默认结果行数上限。"""


# ── 文件工具限制 ─────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/actions/tools/local_tool/tools.py
# 建议 YAML 路径: TOOLS.read_max_file_size / read_max_output_bytes / diff_max_chars / skip_dirs

DEFAULT_READ_MAX_FILE_SIZE: int = 262144
"""全文件读取阈值（字节，256 KB）。超过此大小的文件只做 head/tail 读取。"""

DEFAULT_READ_MAX_OUTPUT_BYTES: int = 102400
"""Read 工具单次读取的最大输出预算（字节，100 KB）。"""

DEFAULT_DIFF_MAX_CHARS: int = 8000
"""Edit 工具差异输出的最大字符数。"""

DEFAULT_SKIP_DIRS: frozenset[str] = frozenset({".git", ".svn", ".hg", "__pycache__", "node_modules", ".venv", ".tox"})
"""Glob 和 Grep 工具默认跳过的目录名集合。"""

# ── 工具结果截断 ─────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/utils/messages_utils.py
# 建议 YAML 路径: 节点级 max_tool_result_length (Flex executor 已支持)

DEFAULT_MAX_TOOL_RESULT_LENGTH: int = 8192
"""发给 LLM 的工具结果内容截断长度（字符）。Flex executor 可通过节点配置
``max_tool_result_length`` 覆盖，默认值由此常量提供。"""


# ── Metadata Tracker ─────────────────────────────────────────────────────────
# 当前定义位置: dataagent/core/flex/hooks/metadata_tracker.py
# 建议 YAML 路径: AGENT_CONFIG.metadata_tool_args_max_bytes / metadata_description_max_bytes

DEFAULT_METADATA_TOOL_ARGS_MAX_BYTES: int = 1024
"""文件元数据中存储工具参数的最大字节数。"""

DEFAULT_METADATA_DESCRIPTION_MAX_BYTES: int = 256
"""文件元数据中描述文本的最大字节数。"""

DEFAULT_METADATA_TRUNCATION_SUFFIX: str = " ...(truncated)"
"""元数据截断时的后缀标记。"""


# ── 并发控制 ─────────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/actions/tools/concurrency.py
# 建议 YAML 路径: AGENT_CONFIG.cpu_buffer / max_concurrency_cap / min_concurrency

DEFAULT_CPU_BUFFER: int = 4
"""CPU 核心数基础上的额外并发数。"""

DEFAULT_MAX_CONCURRENCY_CAP: int = 16
"""并发数绝对上限，即使 CPU 核心数很多也不会超过此值。"""

DEFAULT_MIN_CONCURRENCY: int = 1
"""并发数最小值，低配机器至少保持的并发度。"""


# ── MCP / A2A 工具发现超时 ──────────────────────────────────────────────────
# 当前定义位置: dataagent/core/managers/action_manager/manager.py
# 建议 YAML 路径: TOOLS.mcp_discovery_timeout / mcp_cleanup_timeout

DEFAULT_MCP_DISCOVERY_TIMEOUT: float = 60.0
"""MCP / A2A 工具自动发现时的 future.result() 超时（秒）。"""

DEFAULT_MCP_CLEANUP_TIMEOUT: float = 5.0
"""MCP / A2A 注册表清理时 asyncio.wait_for 超时（秒）。"""


# ── 沙箱默认值 ───────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/actions/tools/local_tool/sandbox.py
# 建议 YAML 路径: SANDBOX.ro_binds / tmpfs_paths

DEFAULT_SANDBOX_RO_BINDS: list[str] = ["/usr", "/lib", "/lib64", "/bin", "/sbin"]
"""bwrap 沙箱默认只读挂载路径。"""

DEFAULT_SANDBOX_TMPFS_PATHS: list[str] = ["/tmp"]
"""bwrap 沙箱默认 tmpfs 挂载路径。"""


# ============================================================================
# NL2SQL与IR与知识图谱
# ============================================================================


# ── IR 消息消费者 ────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/utils/converter/ir_message_consumer.py
# 建议 YAML 路径: AGENT_CONFIG.ir_recent_turns / ir_knowledge_max_len / ir_script_max_len

DEFAULT_IR_RECENT_TURNS: int = 10
"""IR 摘要系统中保留完整 ToolMessage 内容的最近轮次数。"""

DEFAULT_IR_KNOWLEDGE_MAX_LEN: int = 300
"""IR 摘要中 Knowledge 节点内容的最大字符数。"""

DEFAULT_IR_SCRIPT_MAX_LEN: int = 200
"""IR 摘要中 Script 节点内容预览的最大字符数。"""


# ── IR 转换器 ────────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/utils/converter/ir_converter_constants.py
#  和 dataagent/utils/converter/result_ir_converter.py
# 建议 YAML 路径: AGENT_CONFIG.ir_knowledge_min_len / ir_max_file_chars / ir_max_path_len

DEFAULT_IR_KNOWLEDGE_MIN_LENGTH: int = 500
"""Knowledge 节点创建的最小字符阈值。"""

DEFAULT_IR_MAX_FILE_CHARS: int = 10000
"""IR 转换时 _safe_read_file 默认最大读取字符数。"""

DEFAULT_IR_MAX_PATH_LEN: int = 256
"""IR 转换时路径字符串最大长度。"""

DEFAULT_IR_COLUMN_SAMPLE_ROWS: int = 100
"""IR 转换时 DataFrame 列值采样最大行数。"""

DEFAULT_IR_COLUMN_UNIQUE_SAMPLES: int = 20
"""IR 转换时每列最大唯一样本值数。"""


# ── NL2SQL ───────────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/agents/nl2sql/ 下各文件
# 建议 YAML 路径: NL2SQL.* 或 AGENT_CONFIG.nl2sql_*

NL2SQL_PROMPT_PREFIX = "agents/nl2sql/prompts"
"""NL2SQL 提示词目录相对 dataagent 路径"""

DEFAULT_NL2SQL_REFLECTOR_THRESHOLD: float = 0.9
"""NL2SQL Reflector 节点结果评分接受阈值。"""

DEFAULT_NL2SQL_SELECTOR_THRESHOLD: float = 0.9
"""NL2SQL Selector 节点 SQL 选择评分阈值。"""

DEFAULT_NL2SQL_NUM_SAMPLES: int = 3
"""NL2SQL Generator 每种策略生成的 SQL 样本数。"""

DEFAULT_NL2SQL_NUM_WORKERS: int = 1
"""NL2SQL Generator 线程池工作线程数。"""

DEFAULT_NL2SQL_REF_RETRIES: int = 2
"""NL2SQL Reflector 反思循环默认重试次数。"""

DEFAULT_NL2SQL_SEL_RETRIES: int = 1
"""NL2SQL Selector 选择循环默认重试次数。"""

DEFAULT_NL2SQL_SCHEMA_TOP_K: int = 1
"""NL2SQL Perceptor Schema 链接默认 Top-K。"""

DEFAULT_NL2SQL_PREVIEW_LIMIT: int = 5
"""NL2SQL Executor 查询结果预览行数。"""

DEFAULT_NL2SQL_CELL_TRUNCATE_LENGTH: int = 500
"""NL2SQL 工具中单元格值的截断长度。"""

DEFAULT_NL2SQL_METAVISOR_COLUMN_LIMIT: int = 1000
"""Metavisor 列检索默认上限。"""

DEFAULT_NL2SQL_VALUEMATCH_TOP_K: int = 3
"""Metavisor ValueMatch 默认 Top-K。"""

DEFAULT_NL2SQL_SQLITE_TIMEOUT: int = 30
"""NL2SQL SQLite 查询超时（秒）。"""

DEFAULT_NL2SQL_SQLITE_PROGRESS_INTERVAL: int = 10000
"""SQLite 进度处理器回调间隔（虚拟机器指令数）。"""


# ── 内置工具注册 ─────────────────────────────────────────────────────────────
# 完整工具目录见 dataagent/core/managers/action_manager/manager.py（_BUILTIN_LOCAL_TOOL_CATALOG）
# 此处仅声明默认启用的工具名（与目录取交集）；YAML 可用 TOOLS.builtin 覆盖（含 [] 表示不注册）

DEFAULT_BUILTIN_SKILL_NAMES: frozenset[str] = frozenset({})
"""始终有资格被发现的内置 Skill 名称集合。"""

DEFAULT_BUILTIN_LOCAL_TOOLS: tuple[str, ...] = (
    "bash",
    "edit_file",
    "read_file",
    "write_file",
    "grep",
    "glob",
    "create_plan",
    "update_plan",
    "delete_plan",
    "complete_current_todo",
)
"""默认注册的本地工具模块名列表。"""


# ============================================================================
# UI渲染与可视化
# ============================================================================


# ── Rich 渲染器 ──────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/utils/cli/rich_renderer.py
# 建议 YAML 路径: DISPLAY.*

DEFAULT_INITIAL_THINKING_MIN_DISPLAY_SECONDS: float = 1.0
"""Rich 渲染器中 "思考中" 转轮最短显示时间（秒）。"""

DEFAULT_PLANNER_REFRESH_INTERVAL_SECONDS: float = 0.05
"""Planner 流式输出面板刷新间隔（秒）。"""

DEFAULT_LIVE_REFRESH_PER_SECOND: int = 12
"""Rich Live 面板刷新频率。"""

DEFAULT_MAX_SUBAGENT_HINT_LINES: int = 5
"""终端显示的子 Agent 进度最大行数。"""

DEFAULT_RICH_SCALAR_MAX_LENGTH: int = 120
"""Rich 树状视图中标量值截断长度。"""

DEFAULT_RICH_RESULT_TRUNCATION: int = 500
"""Rich 渲染中工具结果体截断字符数。"""

DEFAULT_RICH_ERROR_TRUNCATION: int = 160
"""Rich 渲染中错误文本截断字符数。"""


# ── 上下文可视化 ─────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/core/context/utils_context_trajectory.py
# 建议 YAML 路径: CONTEXT.vis.*

DEFAULT_CONTEXT_NODE_SIZE_DOT: int = 20
DEFAULT_CONTEXT_NODE_SIZE_BOX: int = 25
DEFAULT_CONTEXT_EDGE_LENGTH: int = 180
"""Pyvis 网络图默认边长度。"""

DEFAULT_CONTEXT_NETWORK_HEIGHT: str = "800px"
DEFAULT_CONTEXT_NETWORK_WIDTH: str = "100%"

DEFAULT_CONTEXT_STABILIZATION_ITERATIONS: int = 1000
"""Pyvis 网络图稳定化迭代次数。"""

DEFAULT_CONTEXT_MAX_NODE_TITLE_LEN: int = 180
"""Context 图中节点标题最大长度。"""


# ============================================================================
# 数据库与运行时探测
# ============================================================================


# ── 数据库探测 ───────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/core/cbb/runtime_env.py
# 建议 YAML 路径: DATABASE.connect_probe_timeout / process_probe_timeout

DEFAULT_DB_CONNECT_PROBE_TIMEOUT: int = 3
"""数据库连接可用性探测超时（秒）。"""

DEFAULT_DB_PROCESS_PROBE_TIMEOUT: int = 5
"""数据库进程等待超时（秒）。"""

DEFAULT_DB_PROCESS_KILL_TIMEOUT: int = 1
"""数据库进程终止后清理等待超时（秒）。"""


# ── CPU 采样 ─────────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/core/cbb/runtime_env.py

DEFAULT_CPU_SAMPLE_SECONDS: float = 0.5
"""CPU 使用率采样间隔（秒）。"""


# ============================================================================
# 环境默认值与内置注册
# ============================================================================


# ── 默认回退值 ───────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/core/flex/agent.py
# 建议 YAML 路径: 这些已有 YAML 对应字段 (USER_ID / SESSION_ID / RUN_ID / SUB_ID)

DEFAULT_USER_ID: str = "anonymous"
"""未指定时的默认用户 ID。YAML USER_ID 已可覆盖。"""

DEFAULT_SESSION_ID: str = "default_session"
"""未指定时的默认会话 ID。YAML SESSION_ID 已可覆盖。"""

DEFAULT_RUN_ID: int = 0
"""未指定时的默认运行 ID。YAML RUN_ID 已可覆盖。"""

DEFAULT_SUB_ID: int = 0
"""未指定时的默认子 Agent ID。YAML SUB_ID 已可覆盖。"""

DEFAULT_BACKEND: str = "langgraph"
"""未指定时的默认后端引擎。YAML AGENT_CONFIG.backend 已可覆盖。"""

DEFAULT_MODE: str = "chat"
"""未指定时的默认运行模式。DataAgent 属性 setter 已可控制。"""


# ── Context Trajectory TodoList ──────────────────────────────────────────────
# 当前定义位置: dataagent/core/context/todolist_manager.py, context_trajectory.py

DEFAULT_TODOLIST_MAXLEN: int = 100
"""Context 中待办事项列表最大条目数。"""


# ============================================================================
# 聚合导出 — 按用途分组，方便外部一次性引用
# ============================================================================

# 压缩全套
COMPRESSION_DEFAULTS: dict[str, int | float] = {
    "token_limit": DEFAULT_COMPRESS_TOKEN_LIMIT,
    "message_cnt": DEFAULT_COMPRESS_MESSAGE_CNT,
    "fold_temperature": DEFAULT_COMPRESS_FOLD_TEMPERATURE,
    "max_retries": DEFAULT_COMPRESS_MAX_RETRIES,
}

# 工具超时全套
TOOL_TIMEOUT_DEFAULTS: dict[str, int] = {
    "bash": DEFAULT_BASH_TIMEOUT,
    "subagent": DEFAULT_SUBAGENT_TOOL_TIMEOUT,
    "grep": DEFAULT_GREP_TIMEOUT,
    "mcp_discovery": int(DEFAULT_MCP_DISCOVERY_TIMEOUT),
}

# 文件工具限制全套
FILE_TOOL_LIMITS: dict[str, int] = {
    "read_max_file_size": DEFAULT_READ_MAX_FILE_SIZE,
    "read_max_output_bytes": DEFAULT_READ_MAX_OUTPUT_BYTES,
    "diff_max_chars": DEFAULT_DIFF_MAX_CHARS,
}

# 并发全套
CONCURRENCY_DEFAULTS: dict[str, int] = {
    "cpu_buffer": DEFAULT_CPU_BUFFER,
    "max_concurrency_cap": DEFAULT_MAX_CONCURRENCY_CAP,
    "min_concurrency": DEFAULT_MIN_CONCURRENCY,
}

# IR 转换全套
IR_CONVERTER_DEFAULTS: dict[str, int] = {
    "recent_turns": DEFAULT_IR_RECENT_TURNS,
    "knowledge_max_len": DEFAULT_IR_KNOWLEDGE_MAX_LEN,
    "script_max_len": DEFAULT_IR_SCRIPT_MAX_LEN,
    "knowledge_min_len": DEFAULT_IR_KNOWLEDGE_MIN_LENGTH,
    "max_file_chars": DEFAULT_IR_MAX_FILE_CHARS,
    "max_path_len": DEFAULT_IR_MAX_PATH_LEN,
    "column_sample_rows": DEFAULT_IR_COLUMN_SAMPLE_ROWS,
    "column_unique_samples": DEFAULT_IR_COLUMN_UNIQUE_SAMPLES,
}

# NL2SQL 全套
NL2SQL_DEFAULTS: dict[str, float | int] = {
    "reflector_threshold": DEFAULT_NL2SQL_REFLECTOR_THRESHOLD,
    "selector_threshold": DEFAULT_NL2SQL_SELECTOR_THRESHOLD,
    "num_samples": DEFAULT_NL2SQL_NUM_SAMPLES,
    "num_workers": DEFAULT_NL2SQL_NUM_WORKERS,
    "ref_retries": DEFAULT_NL2SQL_REF_RETRIES,
    "sel_retries": DEFAULT_NL2SQL_SEL_RETRIES,
    "schema_top_k": DEFAULT_NL2SQL_SCHEMA_TOP_K,
    "preview_limit": DEFAULT_NL2SQL_PREVIEW_LIMIT,
    "cell_truncate_len": DEFAULT_NL2SQL_CELL_TRUNCATE_LENGTH,
    "sqlite_timeout": DEFAULT_NL2SQL_SQLITE_TIMEOUT,
}
