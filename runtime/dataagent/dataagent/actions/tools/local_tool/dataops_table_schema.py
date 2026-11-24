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

「数据质量校验」不一定要有一条 INSERT 语句。字段可以从四种来源解析出来：

=================  =======================================================
来源               怎么拿字段
=================  =======================================================
用户显式指定        直接给字段名（``fields=[...]``）—— **元数据缺表时的兜底**
DDL (CREATE)      从 ``CREATE TABLE ... ( col TYPE, ... )`` 里解析
表名              通过 semantic-service 查表的列元数据
DML (INSERT)      从 ``INSERT ... SELECT`` 的投影里解析（既有逻辑）
=================  =======================================================

**为什么要有「用户显式指定」这一档**：元数据（semantic-service）里**没有**
这张表，但数据库里**实际有** —— 新表刚上线、元数据同步有延迟、或者这张表
压根不在元数据采集范围内。早先这里一失败整条校验就断了（``schema_unavailable``），
用户明明能提供字段却做不了试算。现在用户直接把字段告诉工具（给 DDL 或者
直接列字段名），校验就能跑起来。

四种来源最终都收敛成同一对数据：**字段名列表** + **字段类型映射**
（类型决定判空写法 —— STRING 用 ``bicoredata.IsEmpty``，数值类用
``IS NOT NULL``）。

对应 skill：``de_agent/skill/dataops_validate_data/SKILL.md``。
"""

from __future__ import annotations

import re
from collections.abc import Callable
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


def extract_ddl_table_ref(ddl_sql: str) -> str | None:
    """从 DDL 里解析出表名 —— **裸表名也认**。

    与 :func:`extract_ddl_target_table` 的区别：那个只认带库名的
    ``db.table``（直连真实目标表用，裸表名依赖引擎当前库，不可靠）；
    这个**认裸表名**（``CREATE TABLE ads_x``），用在「按目标表匹配
    workspace 里的 DDL」这类场景。

    **为什么必须有它**：交付物的 DDL 常常不带库名（``CREATE TABLE ads_x``），
    而 DML 里是 ``biads.ads_x``。只认带库名的解析器在这种 DDL 上返回
    ``None`` → 匹配直接失败 → 代码退回「按 mtime 取最新」→ **拿到的
    可能是另一张表的 DDL** → 建出来的临时表 schema 和写入的数据对不上。

    Returns:
        表名（``db.table`` 或裸表名）；解析不出时返回 ``None``。
    """
    if not ddl_sql:
        return None
    m = _CREATE_TABLE_RE.search(_strip_comments(ddl_sql))
    if not m:
        return None
    ref = _strip_quotes(m.group(1))
    return ref or None


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


# ============================================================================
# 用户显式指定的字段（新增：元数据缺表时的兜底）
# ============================================================================
#
# **为什么需要这条来源**：元数据（semantic-service）里**没有**这张表，但数据库
# 里**实际有** —— 这是生产上很常见的状态（新表刚上线、元数据同步有延迟、
# 或者这张表压根不在元数据采集范围内）。
#
# 早先的直连路径只认 semantic 元数据：查不到就整条校验失败（``schema_unavailable``），
# 用户明明能提供字段却做不了试算。现在多一条来源：**用户直接把字段告诉工具**
# （给 DDL 或者直接列字段名），让校验能跑起来。
#
# **优先级：用户显式 > DDL > semantic 元数据**。用户显式给的字段是**用户当下
# 最确定的口径** —— 他亲眼看着这张表。当然，用户也可能给错，所以字段名必须
# 过白名单（见 :func:`validate_user_fields`）。

#: 字段名白名单 —— 单段标识符 ``[A-Za-z_][A-Za-z0-9_]*``。
#:
#: **不接受** ``db.tbl.col``（带库表前缀）与 ``*``：
#: - 前缀会被反引号原样包成 `` `db.tbl.col` ``，后端解析不出来；
#: - ``*`` 是 DataOps 平台**硬性禁止**的（且会让「按字段统计覆盖率」失去意义）。
_SAFE_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 一次最多接受多少个字段 —— 防止 LLM 传进来几百个字段把校验 SQL 撑爆。
_MAX_USER_FIELDS = 500


class UserFieldError(ValueError):
    """用户显式指定的字段不合法 —— 消息可以直接展示给人看。"""


def validate_user_fields(fields: Any) -> list[str]:
    """校验并归一化用户显式指定的字段名列表。

    Args:
        fields: 用户给的字段名。可以是 ``list[str]``，也可以是**逗号/空格/
        换行分隔的字符串**（LLM 常这么传）—— 字符串会被拆开。

    Returns:
        去重后的字段名列表（保持原顺序，大小写**保留** —— 列名大小写要跟
        数据库里的实际写法一致）。

    Raises:
        UserFieldError: 字段名不合法（不是合法标识符、含库表前缀、是 ``*``、
            数量超限、或不是列表/字符串）。
    """
    if fields is None:
        return []

    # 字符串 → 按逗号/空白/换行拆开。
    if isinstance(fields, str):
        raw_items = [part for part in re.split(r"[,\s]+", fields.strip()) if part]
    elif isinstance(fields, (list, tuple)):
        raw_items = list(fields)
    else:
        raise UserFieldError(
            f"fields 必须是字段名列表或逗号分隔的字符串，实际是 {type(fields).__name__}。"
        )

    if not raw_items:
        return []

    if len(raw_items) > _MAX_USER_FIELDS:
        raise UserFieldError(
            f"字段数量 {len(raw_items)} 超过上限 {_MAX_USER_FIELDS} —— "
            "请只列出需要校验的字段。"
        )

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, str):
            raise UserFieldError(f"字段名必须是字符串，实际是 {type(item).__name__}：{item!r}")
        name = item.strip().strip("`")
        if not name:
            continue
        if name == "*":
            raise UserFieldError(
                "字段名不能是 `*` —— DataOps 平台禁止 SELECT *，"
                "请显式列出需要校验的字段名。"
            )
        if "." in name:
            raise UserFieldError(
                f"字段名 `{name}` 不能带库/表前缀 —— 请只写列名（如 `{name.split('.')[-1]}`）。"
            )
        if not _SAFE_COLUMN_RE.match(name):
            raise UserFieldError(
                f"非法字段名 `{name}` —— 字段名只允许单段 [A-Za-z_][A-Za-z0-9_]*。"
            )
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        cleaned.append(name)
    return cleaned


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
    fields, column_types, _primary_keys = fetch_semantic_columns_and_primary_keys(
        table_name,
        _tool_context=_tool_context,
    )
    return fields, column_types


def fetch_semantic_primary_key_columns(
    table_name: str,
    *,
    _tool_context: ToolExecutionContext,
) -> tuple[str, ...]:
    """只取 semantic 元数据里标记为主键的列。

    ``DataTaskIR`` 的口径声明（``identity.components``）只在走 IR 链路的会话里
    才有。独立的数据校验（用户输入「执行数据校验」）根本没建 IR → 拿不到任何
    主键声明 → 「主键重复率」和「抽样回源比对」这两项**整项跳过** —— 而它们
    恰恰是正确性校验里最硬的两条。

    semantic-service 的列元数据里带着主键标记（``is_primary_key``），这是物理
    表层面的权威声明 —— 拿它兜底，主键重复率就能跑起来。

    Returns:
        主键列名元组（按元数据里的列顺序）；表不存在 / 没有主键 / 查询失败
        时返回空元组 —— 调用方据此降级，**不猜**。
    """
    try:
        _fields, _types, primary_keys = fetch_semantic_columns_and_primary_keys(
            table_name,
            _tool_context=_tool_context,
        )
    except SchemaResolutionError:
        return ()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[validate_data] semantic primary key lookup failed: {exc!r}")
        return ()
    return primary_keys


def fetch_semantic_columns_and_primary_keys(
    table_name: str,
    *,
    _tool_context: ToolExecutionContext,
) -> tuple[list[str], dict[str, str], tuple[str, ...]]:
    """查表的列元数据 + 主键列（一次请求拿到三样东西）。

    Returns:
        ``(字段名列表, {字段名: 小写类型}, 主键列名元组)``。

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
    primary_keys: list[str] = []
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
            # 主键标记 —— semantic-service 用它标注列是否主键。
            if _is_primary_key_meta(meta) and column not in primary_keys:
                primary_keys.append(column)
    except Exception as exc:  # noqa: BLE001
        raise SchemaResolutionError(f"解析表 `{table_name}` 的字段元数据失败：{exc}") from exc

    if not fields:
        raise SchemaResolutionError(f"表 `{table_name}` 在元数据中不存在或没有字段。")

    logger.debug(
        f"[validate_data] semantic columns for {table_name}: {len(fields)} "
        f"(primary keys: {primary_keys})"
    )
    return fields, column_types, tuple(primary_keys)


#: semantic 元数据里「是否主键」的候选 key —— 不同版本的拼法不一致，都认。
_SEMANTIC_PRIMARY_KEY_KEYS: tuple[str, ...] = (
    "is_primary_key",
    "isPrimaryKey",
    "primary_key",
    "is_pk",
)


def _is_primary_key_meta(meta: dict[str, Any]) -> bool:
    """元数据里的这列是不是主键。

    只认明确的真值 —— ``"false"`` 这种字符串在 Python 里是**真值**（非空
    字符串），直接 ``bool()`` 判断会把「明确不是主键」的列误判成主键 —— 那会
    让主键重复率用上一组错误的列去算，结论完全错。
    """
    for key in _SEMANTIC_PRIMARY_KEY_KEYS:
        if key not in meta:
            continue
        value = meta.get(key)
        if isinstance(value, bool):
            if value:
                return True
            continue
        if isinstance(value, str):
            if value.strip().lower() in {"true", "1", "yes"}:
                return True
            continue
        if isinstance(value, (int, float)):
            if value:
                return True
            continue
    return False


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


# ============================================================================
# 统一字段解析（多来源兜底）
# ============================================================================


def resolve_columns(
    *,
    target_table: str = "",
    user_fields: Any = None,
    ddl_sql: str = "",
    _tool_context: ToolExecutionContext,
    semantic_columns_fetcher: Callable[..., tuple[list[str], dict[str, str]]] | None = None,
    semantic_primary_keys_fetcher: Callable[..., tuple[str, ...]] | None = None,
) -> tuple[list[str], dict[str, str], tuple[str, ...], str]:
    """解析校验对象表的字段 —— **多来源兜底**，任何一个能用就行。

    **来源优先级**（先到先得）：

    1. **用户显式指定的字段**（``user_fields``）—— 用户亲眼看着这张表，
       他给的口径最可信。元数据里查不到表时，这是唯一能救场的来源；
    2. **用户给的建表 DDL**（``ddl_sql``）—— 字段名 + **列类型**都有，
       比光有字段名更好（列类型决定判空写法）；
    3. **semantic 元数据** —— 平台侧的权威列清单，**还能带主键标记**。

    **为什么不让「查不到就失败」**：元数据（semantic-service）里没有这张表，
    但数据库里**实际有** —— 新表刚上线、元数据同步延迟、或者这张表压根不在
    采集范围内。早先这里一失败整条校验就断了，用户明明能提供字段却做不了试算。

    Args:
        target_table: 目标表 ``db.table``（semantic 查询用）。
        user_fields: 用户显式给的字段名（列表或逗号分隔字符串）。
        ddl_sql: 用户给的建表 DDL。
        _tool_context: 工具执行上下文。
        semantic_columns_fetcher: 查 semantic 列元数据的可调用对象。留空用模块
            内的默认实现。**注入点** —— 调用方（工具层）把自己的模块级名字传
            进来，测试就能在**调用方**那个模块上打桩，而不必打桩到本模块。
        semantic_primary_keys_fetcher: 查 semantic 主键标记的可调用对象。
            同上。

    Returns:
        ``(字段名列表, {字段名: 小写类型}, 主键列元组, 来源标记)``。
        来源标记取 ``"user_fields"`` / ``"ddl"`` / ``"semantic"``，
        用于报告里说清「字段是从哪来的」。

    Raises:
        UserFieldError: 用户给的字段名不合法。
        SchemaResolutionError: **所有来源都拿不到**字段。
    """
    columns_fetcher = semantic_columns_fetcher or fetch_semantic_columns
    primary_keys_fetcher = semantic_primary_keys_fetcher or fetch_semantic_primary_key_columns

    # --- 1) 用户显式字段（最高优先级）-----------------------------------
    explicit = validate_user_fields(user_fields)
    if explicit:
        # 列类型：DDL 里有就用 DDL 的（判空写法要靠它），否则类型未知 →
        # 退回 ``IS NOT NULL``（对所有类型都成立）。
        column_types = parse_ddl_column_types(ddl_sql) if ddl_sql else {}
        # **只保留用户给的字段** —— DDL 里可能还有分区列和其它列，
        # 用户没列出来的字段不参与校验（用户要校验什么就是什么）。
        resolved_types = {
            name: column_types[name]
            for name in explicit
            if name in column_types
        }
        # DDL 的列名大小写可能与用户写的不一致 —— 按大小写不敏感补一次。
        for name in explicit:
            if name in resolved_types:
                continue
            for ddl_name, ddl_type in column_types.items():
                if ddl_name.lower() == name.lower():
                    resolved_types[name] = ddl_type
                    break
        # 主键：DDL 里没有主键声明 —— 用户没给主键就不猜。
        # 仍可以试一次 semantic（那张表在元数据里可能**有**主键标记，
        # 只是「表本身」查不到 —— 不太可能，但代价只是一次请求）。
        primary_keys = _try_semantic_primary_keys(
            target_table, _tool_context, fetcher=primary_keys_fetcher
        )
        return explicit, resolved_types, primary_keys, "user_fields"

    # --- 2) 用户给的 DDL --------------------------------------------------
    if ddl_sql:
        fields, column_types = parse_ddl_columns(ddl_sql)
        if fields:
            primary_keys = _try_semantic_primary_keys(
                target_table, _tool_context, fetcher=primary_keys_fetcher
            )
            return fields, column_types, primary_keys, "ddl"

    # --- 3) semantic 元数据 ----------------------------------------------
    if target_table:
        fields, column_types = columns_fetcher(
            target_table,
            _tool_context=_tool_context,
        )
        # 主键标记单独取 —— ``fetch_semantic_columns`` 不返回主键（那是
        # ``fetch_semantic_primary_key_columns`` 的职责）。
        primary_keys = _try_semantic_primary_keys(
            target_table, _tool_context, fetcher=primary_keys_fetcher
        )
        return fields, column_types, primary_keys, "semantic"

    raise SchemaResolutionError(
        "未提供目标表名，也没有可用的字段来源 —— "
        "请传入 `target_table`（表名）或 `fields`（字段名列表）。"
    )


def _try_semantic_primary_keys(
    target_table: str,
    _tool_context: ToolExecutionContext,
    fetcher: Callable[..., tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """尽最大努力拿 semantic 主键标记；拿不到就返回空元组（**绝不抛异常**）。

    用户显式给了字段时，主键**只能**从 semantic 侧来（用户没给主键、
    DDL 里也没有主键声明）。查不到就空着 —— 调用方据此跳过主键相关校验项
    并注明原因，**不猜**。
    """
    table = (target_table or "").strip()
    if not table or table.startswith("adhoctemp."):
        return ()
    lookup = fetcher or fetch_semantic_primary_key_columns
    try:
        return lookup(
            table,
            _tool_context=_tool_context,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[table_schema] semantic primary key lookup failed: {exc!r}")
        return ()
