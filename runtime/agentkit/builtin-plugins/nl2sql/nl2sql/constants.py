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
"""Plugin-local constants for the nl2sql sub-agent (ported from the legacy ``dataagent`` constants)."""

from datetime import timedelta, timezone

TZ_CN = timezone(timedelta(hours=8))

# NL2SQL 提示词目录，相对本包（nl2sql/）根目录。
NL2SQL_PROMPT_PREFIX = "prompts"

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

DEFAULT_NL2SQL_MODEL_CALL_BUDGET: int = 64
"""NL2SQL 单次任务内模型调用的默认预算上限。"""

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
"""NL2SQL SQLite 进度回调间隔（虚拟机指令数）。"""

DEFAULT_WORKFLOW_RECURSION_LIMIT: int = 200
"""LangGraph 递归上限（与 legacy ``dataagent`` 默认一致）。"""
