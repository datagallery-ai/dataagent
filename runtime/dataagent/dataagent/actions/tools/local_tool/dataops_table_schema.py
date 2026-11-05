# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Resolve the columns of a table for the standalone data-validation tool.

「数据质量校验」不一定要有一条 INSERT 语句。字段可以从三种来源解析出来：

============  ==========================================================
来源           怎么拿字段
============  ==========================================================
DML (INSERT)  从 ``INSERT ... SELECT`` 的投影里解析（既有逻辑）
DDL (CREATE)  从 ``CREATE TABLE ... ( col TYPE, ... )`` 里解析
表名          通过 semantic-service 查表的列元数据
============  ==========================================================

三种来源最终都收敛成同一对数据：**字段名列表** + **字段类型映射**
（类型决定判空写法 —— STRING 用 ``bicoredata.IsEmpty``，数值类用
``IS NOT NULL``）。

对应 skill：``de_agent/skill/dataops_validate_data/SKILL.md``。
"""

from __future__ import annotations

import re
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.dataops_quality_check import (
    DEFAULT_EXCLUDE_FIELDS,
    parse_ddl_column_types,
)
from dataagent.utils.log import logger

# ============================================================================
# DDL 识别与解析
# ============================================================================

#: ``CREATE [EXTERNAL] TABLE [IF NOT EXISTS] <table>`` —— **不要求表名带库名**。
#:
#: 识别和取表名是**两件事**：
#:   - 识别（:func:`looks_like_ddl`）：只要长得像建表语句就算 —— 哪怕表名是裸的
#:     ``ads_x``（不带库名）。否则调用方传了 ``target_table`` + 一段裸表名 DDL 时，
#:     会误走 semantic 路径，把用户手上现成的 DDL 丢掉。
#:   - 取表名（:func:`extract_ddl_target_table`）：只认带库名的 ``db.table`` ——
#:     独立数据验证直连的是真实目标表，裸表名依赖引擎的当前库上下文，不可靠。
_CREATE_TABLE_RE = re.compile(
    r"\bCREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([`\w.-]+(?:\.[`\w.-]+)?)",
    re.IGNORECASE,
)

#: 只认**带库名**的表名（``db.table``）。
_QUALIFIED_TABLE_RE = re.compile(
    r"\bCREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([`\w.-]+(?:\.[`\w.-]+)+)",
    re.IGNORECASE,
)


def _strip_comments(sql: str) -> str:
    """去掉块注释和行注释。

    行注释必须一起剥 —— 否则 ``-- create table foo`` 这种注释会让
    :func:`looks_like_ddl` 误判。
    """
    text = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)
    return text


def looks_like_ddl(sql: str) -> bool:
    """SQL 是否是建表语句（``CREATE [EXTERNAL] TABLE ...``）。"""
    if not sql:
        return False
    return bool(_CREATE_TABLE_RE.search(_strip_comments(sql)))


def _strip_quotes(ref: str) -> str:
    """剥掉标识符各段上的引号。

    ``\\`biads\\`.\\`ads_x\\``` → ``biads.ads_x``。不能只 ``strip("`")`` ——
    那样只会剥掉首尾的反引号，中间那个 ``\\` `` 会留下来，变成
    ``biads`.`ads_x`` —— 拿这个名字去查表必然找不到。
    """
    return ".".join(part.strip().strip("`").strip('"').strip("'") for part in ref.split("."))


def extract_ddl_target_table(ddl_sql: str) -> str | None:
    """从 DDL 里解析出表名（``db.table``）。

    只认**带库名**的表名（``db.table``）—— 独立数据验证直连的是真实目标表，
    裸表名依赖引擎的当前库上下文，跨环境不可靠。解析不出时返回 ``None``，
    由调用方提示用户显式传 ``target_table``。
    """
    if not ddl_sql:
        return None
    m = _QUALIFIED_TABLE_RE.search(_strip_comments(ddl_sql))
    if not m:
        return None
    ref = _strip_quotes(m.group(1))
    return ref or None


def parse_ddl_columns(ddl_sql: str) -> tuple[list[str], dict[str, str]]:
    """从 Hive DDL 解析出 ``(字段名列表, {字段名: 小写类型})``。

    分区列（``PARTITIONED BY`` 里的列）不返回 —— 它们不是业务字段，
    做覆盖率统计没有意义。
    """
    column_types = parse_ddl_column_types(ddl_sql)
    return list(column_types.keys()), column_types


# ============================================================================
# Semantic layer 解析
# ============================================================================


class SchemaResolutionError(RuntimeError):
    """字段解析失败 —— 带上可以直接展示给人看的原因。"""


def _normalize_type(raw: Any) -> str:
    """把 semantic-service 的 ``value_type`` 归一化成小写类型名。

    ``varchar(32)`` / ``char(10)`` 归到 ``string`` —— 它们是字符串存储，
    判空要用 ``bicoredata.IsEmpty``；其余（``bigint`` / ``timestamp`` 等）
    原样小写保留，判空走 ``IS NOT NULL``。
    """
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if text.startswith(("string", "varchar", "char")):
        return "string"
    return text


def fetch_semantic_columns(
    table_name: str,
    *,
    _tool_context: ToolExecutionContext,
) -> tuple[list[str], dict[str, str]]:
    """通过 semantic-service 查表的列元数据。

    Args:
        table_name: 全限定表名 ``db.table``。

    Returns:
        ``(字段名列表, {字段名: 小写类型})``。

    Raises:
        SchemaResolutionError: semantic-service 不可用 / 表不存在 / 表没有字段。
    """
    if not table_name:
        raise SchemaResolutionError("未提供表名。")

    # 延迟导入：semantic-service 客户端依赖配置，测试里不想被牵连。
    from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient

    try:
        client = SemanticServiceClient.from_config(_tool_context.config_manager)
    except Exception as exc:  # noqa: BLE001
        raise SchemaResolutionError(f"semantic-service 未配置或不可用：{exc}") from exc

    try:
        raw = client.get_table_columns_info(table_name, limit=1000)
    except Exception as exc:  # noqa: BLE001
        raise SchemaResolutionError(f"查询表 `{table_name}` 的字段元数据失败：{exc}") from exc

    if not isinstance(raw, dict) or not raw:
        raise SchemaResolutionError(f"表 `{table_name}` 在元数据中不存在或没有字段。")

    fields: list[str] = []
    column_types: dict[str, str] = {}
    # 解析阶段也要兜住 —— 上面的 ``except`` 只覆盖 HTTP 调用本身，
    # 这里的遍历/类型归一化如果碰到形态异常的元数据（比如 ``value_type``
    # 是个会炸的 exotic 对象），会以**原始类型**冒泡穿透调用方的
    # ``except SchemaResolutionError``。本函数的契约是「只抛
    # SchemaResolutionError」，不能有例外。
    try:
        for full_name, meta in raw.items():
            if not isinstance(meta, dict):
                continue
            name = str(full_name).strip()
            # 返回的 key 形如 ``db.table.column`` —— 取最后一段作为列名。
            column = name.split(".")[-1].strip().strip("`")
            if not column:
                continue
            fields.append(column)
            column_types[column] = _normalize_type(meta.get("value_type"))
    except Exception as exc:  # noqa: BLE001
        raise SchemaResolutionError(f"解析表 `{table_name}` 的字段元数据失败：{exc}") from exc

    if not fields:
        raise SchemaResolutionError(f"表 `{table_name}` 在元数据中不存在或没有字段。")

    logger.debug(f"[validate_data] semantic columns for {table_name}: {len(fields)}")
    return fields, column_types


# ============================================================================
# 公共辅助
# ============================================================================


def exclude_columns(
    fields: list[str],
    exclude_fields: frozenset[str] = DEFAULT_EXCLUDE_FIELDS,
) -> list[str]:
    """剔除分区列 / 时间戳 / 主键 ID 等无统计意义的字段。

    排除名单**大小写不敏感** —— semantic 层返回的列名大小写不统一。
    """
    excluded = {str(name).lower() for name in exclude_fields}
    return [name for name in fields if str(name).lower() not in excluded]
