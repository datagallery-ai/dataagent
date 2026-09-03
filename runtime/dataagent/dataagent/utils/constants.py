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
"""Constants shared by the native runtime and retained migration features."""

# ============================================================================
# 压缩与LLM调用
# ============================================================================

from datetime import timedelta, timezone

TZ_CN = timezone(timedelta(hours=8))
"""中国标准时区（UTC+8），供日志时间戳/会话 ID 生成等跨模块使用。"""

# ── 消息压缩 ─────────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/utils/compression_utils.py
# 建议 YAML 路径: AGENT_CONFIG.compress_token_limit

DEFAULT_COMPRESS_TOKEN_LIMIT: int = 32768
"""压缩触发 token 阈值。消息 token 数超过此值 1.2 倍时触发 LLM 折叠压缩。"""

DEFAULT_COMPRESS_MESSAGE_CNT: int = 200
"""压缩触发消息数量阈值。消息数超过此值时触发压缩。"""

DEFAULT_COMPRESS_LOW_WATER_RATIO: float = 0.6
"""分层压缩的统一目标水位；IR candidate 和 Fold 都以触发阈值的 60% 为目标。"""

DEFAULT_COMPRESS_FOLD_TEMPERATURE: float = 0.7
"""语义折叠压缩时 LLM 调用温度，平衡确定性归纳与语义保留。"""

DEFAULT_COMPRESS_MAX_RETRIES: int = 3
"""压缩操作失败时的最大重试次数。"""


# ── LLM 调用重试 ─────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/core/managers/llm_manager/llm_client.py
# YAML 可选: MODEL[*].params.num_retries（覆盖重试次数 N）

DEFAULT_LLM_MAX_RETRIES: int = 5
"""429/Timeout（litellm retry_policy）与 5xx/连接（DataAgent 薄层）的重试次数上限。"""

DEFAULT_LLM_NON_STREAM_TIMEOUT: float = 300.0
"""非流式 LLM 调用（invoke/ainvoke）默认超时（秒）。

无 YAML ``params.timeout`` 与 per-call ``kwargs.timeout`` 时由 ``LLMClient._resolve_timeout`` 注入。
"""

DEFAULT_LLM_STREAM_TIMEOUT: float = 60.0
"""流式 LLM 调用（astream）默认超时（秒）。

无 YAML ``params.timeout`` 与 per-call ``kwargs.timeout`` 时由 ``LLMClient._resolve_timeout`` 注入。
"""

# ============================================================================
# 工具执行与并发控制
# ============================================================================


DEFAULT_SUBAGENT_PROCESS_TIMEOUT: int = 3600
"""内部子 Agent 进程调用的默认超时（秒）。"""

DEFAULT_SUBMIT_SUBAGENT_TIMEOUT_SEC: int = 600
"""``submit_subagent`` 默认 job 超时（秒）。"""

DEFAULT_JOBS_SUBAGENTS_MAX: int = 4
"""Job 路径 subagent 并发上限默认值（``JOBS.subagents.max``）。"""

POLL_WATCH_DEFAULT_INTERVAL_SEC: float = 2.0
"""``poll_subagent`` watch 模式默认轮询间隔（秒）。"""

POLL_WATCH_MAX_WATCH_SEC: int = 120
"""``poll_subagent`` watch 模式最长 watch 秒数。"""

POLL_WATCH_MIN_INTERVAL_SEC: float = 0.5
"""``poll_subagent`` watch 模式最小轮询间隔（秒）。"""

POLL_WATCH_MAX_INTERVAL_SEC: float = 30.0
"""``poll_subagent`` watch 模式最大轮询间隔（秒）。"""

POLL_WATCH_DEFAULT_EVENT_LIMIT: int = 20
"""``poll_subagent`` 单次 poll 默认 events 条数上限。"""

POLL_WATCH_MAX_EVENT_LIMIT: int = 200
"""``poll_subagent`` 单次 poll 最大 events 条数。"""

HUMAN_FEEDBACK_CONDITION_ACTION_SUFFIX: str = (
    "请调用request_human_feedback工具，询问用户是否需要进一步的指导或是否同意操作。"
)
"""``human_feedback_conditions`` 每条条件展开后追加的固定动作说明。"""

MAX_WORKER_METADATA_ARTIFACTS: int = 50
"""Worker ``metadata.json`` 中 ``artifacts`` 路径列表最大条数。

超出时丢弃更早的记录、保留列表末尾（可视作较新的路径）。
当前使用位置: dataagent/core/swarm/worker_metadata.py（``upsert_worker_metadata``）。
"""

WORKER_LOCK_TTL_GRACE_SECONDS: int = 60
"""子 Agent worker 锁 TTL 在 subagent process 超时之外的额外缓冲（秒）。

当前使用位置: dataagent/actions/tools/local_tool/subagent_process.py（``acquire_worker_lock`` 的 ``ttl_seconds``）。
"""

# ── 工具结果截断 ─────────────────────────────────────────────────────────────
# 当前定义位置: dataagent/utils/messages_utils.py
# 建议 YAML 路径: CONTEXT.max_tool_result_length

DEFAULT_MAX_TOOL_RESULT_LENGTH: int = 8192
"""待迁移 Context 路径发送给 LLM 的工具结果内容截断长度。"""


# 当前定义位置: dataagent/core/resource_runtime/mcp.py
# 建议 YAML 路径: RESOURCES[].transport.preflight_timeout_sec

DEFAULT_MCP_PREFLIGHT_TIMEOUT_SEC: int = 5
"""Resource MCP submit 前探活 ping 的默认超时（秒）。"""

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

DEFAULT_NL2SQL_NUM_SAMPLES: int = 1
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

DEFAULT_NL2SQL_SEMANTIC_TABLE_LIST_LIMIT: int = 1000
"""NL2SQL 使用 semantic-service table-list 接口时的默认召回上限。"""

DEFAULT_NL2SQL_SEMANTIC_TABLE_COLUMNS_LIMIT: int = 1000
"""NL2SQL 使用 semantic-service table-columns-info 接口时的默认召回上限。"""

DEFAULT_NL2SQL_SEMANTIC_JOINABLE_TABLES_LIMIT: int = 1000
"""NL2SQL 使用 semantic-service joinable-tables 接口时的默认召回上限。"""

DEFAULT_NL2SQL_SQLITE_TIMEOUT: int = 30
"""NL2SQL SQLite 查询超时（秒）。"""

DEFAULT_NL2SQL_SQLITE_PROGRESS_INTERVAL: int = 10000
"""SQLite 进度处理器回调间隔（虚拟机器指令数）。"""


# ── Semantic Service ─────────────────────────────────────────────────────────
# 当前定义位置: dataagent/actions/tools/semantic_tool/semantic_client.py
# 默认值对齐 semantic-service 接口层 @DefaultValue

DEFAULT_SEMANTIC_SERVICE_TABLE_LIST_LIMIT: int = 25
"""semantic-service table-list 接口默认召回上限。"""

DEFAULT_SEMANTIC_SERVICE_TABLE_COLUMNS_LIMIT: int = 25
"""semantic-service table-columns-info 接口默认召回上限。"""

DEFAULT_SEMANTIC_SERVICE_JOINABLE_TABLES_LIMIT: int = 2000
"""semantic-service joinable-tables 接口默认召回上限。"""

DEFAULT_SEMANTIC_SERVICE_METRIC_COARSE_RECALL_LIMIT: int = 100
"""semantic-service 指标粗召回 fulltext search 默认召回上限。"""

DEFAULT_SEMANTIC_SERVICE_TYPENAME_SEARCH_TOP_K: int = 20
"""semantic-service typeName 全文检索默认召回上限。"""

DEFAULT_SEMANTIC_SERVICE_METRIC_TABLE_COLUMNS_LIMIT: int = 100
"""semantic-service 指标召回按表补充字段时的默认字段上限。"""


# ── 内置工具注册 ─────────────────────────────────────────────────────────────
# 完整工具目录见 dataagent/core/managers/action_manager/manager.py（_BUILTIN_LOCAL_TOOL_CATALOG）
# 此处仅声明默认启用的工具名（与目录取交集）；YAML 可用 TOOLS.builtin 覆盖（含 [] 表示不注册）

DEFAULT_BUILTIN_LOCAL_TOOLS: tuple[str, ...] = (
    "bash",
    "read_file",
    "grep",
    "glob",
)
"""默认注册的本地工具模块名列表。"""


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
# 当前用于待迁移 subagent 路径的身份回退。

DEFAULT_USER_ID: str = "anonymous"
"""未指定时的默认用户 ID。YAML USER_ID 已可覆盖。"""

DEFAULT_SESSION_ID: str = "default_session"
"""未指定时的默认会话 ID。YAML SESSION_ID 已可覆盖。"""

# ── Workspace 框架目录布局 ─────────────────────────────────────────────────────
# 当前定义位置: dataagent/utils/runtime_paths.py（resolve_workspace_layout / resolve_layout_dir）
# 建议 YAML 路径: WORKSPACE_POLICY.layout.<segment>
# 与 WORKSPACE.path 配合：段路径相对于 effective workspace 根；未配置 path 时根为 ~/.dataagent/{user}/{session}/

DEFAULT_WORKSPACE_LAYOUT: dict[str, str] = {
    "session_memory_dir": ".memory",
    "context_dir": ".context",
    "performance_dir": ".performance",
    "workers_dir": "workers",
    "subagents_dir": "subagents",
    "subagent_output_dir": "subagent_output",
    "jobs_dir": "jobs",
    "runtime_dump_dir": ".runtime",
    "tool_outputs_dir": ".dataagent/tool_outputs",
}
"""Session framework artifact paths relative to the effective workspace."""

# workspace 内框架产物子路径标记；用于指代候选过滤，避免将 DATAAGENT_HOME 目录名误判为内部路径。
INTERNAL_ARTIFACT_PATH_MARKERS: tuple[str, ...] = (
    "/.metadata/",
    "/.memory/",
    "/.context/",
    "/.runtime/",
    "/.dataagent/tool_outputs/",
)
"""Session workspace 内框架产物路径片段，用于 ``is_framework_internal_artifact_path`` 等过滤逻辑。"""


META_OVERRIDE_KEYS = "OVERRIDE_KEYS"
"""User YAML meta key listing top-level sections to replace after ``merge_layers``."""
