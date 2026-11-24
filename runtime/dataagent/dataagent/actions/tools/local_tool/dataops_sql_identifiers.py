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
"""DataOps SQL 里「标识符 + 分区过滤条件」的白名单校验与引用。

**为什么需要这个模块**：DataOps 校验链路的 SQL 是**拼**出来的 —— 表名、列名、
分区过滤条件都直接进 SQL 文本。这些值的来源既有平台元数据，也有**用户/agent
显式传入的**（``validate_data(fields=..., ddl=..., partition_filter=...)``）。
只要有一个来源可控，缺了校验就是一条可注入的 SQL。

**两类值，两种白名单**：

1. **标识符**（表名、列名）—— 只允许单段 ``[A-Za-z_][A-Za-z0-9_]*``。
   表名允许多段（``db.tbl``），**每段**都要过；列名只允许**单段** ——
   ``db.tbl.col`` 会被反引号包成 `` `db.tbl.col` ``，后端解析不出来。
   ``*`` 一律拒绝（DataOps 平台硬性禁止 ``SELECT *``）。

2. **分区过滤条件**（``partition_filter``）—— 只允许「列名 = '字面量'」这种
   极窄的形状。DataOps 后端**不**替换 ``$date`` 之类的占位符，工具也不接受
   它们；整个条件串必须是单条比较式，不含 ``;`` / 注释 / 函数调用。

**引用策略**：先校验，再按段加反引号。``db.tbl`` → ``` `db`.`tbl` ``` ——
整体加反引号（``` `db.tbl` ```）在 Hive/ClickHouse 里会被当成**单个**标识符，
跑不通。

**不做什么**：这里**不**校验 SQL 语义，也不解析 SQL。校验不通过时抛
:class:`UnsafeIdentifierError`，由调用方决定怎么表达（工具层把它转成
「已跳过 + 原因」的可读结论）。
"""

from __future__ import annotations

import re

__all__ = [
    "UnsafeIdentifierError",
    "is_safe_identifier",
    "is_safe_column_name",
    "is_safe_table_name",
    "quote_column",
    "quote_table",
    "validate_partition_filter",
]


class UnsafeIdentifierError(ValueError):
    """标识符或过滤条件没通过白名单 —— 消息可以直接展示给人看。"""


#: 单段标识符：字母或下划线开头，后接字母/数字/下划线。
#:
#: 与 :mod:`dataops_probe_tool` 的 ``_SAFE_ID_RE`` 保持一致 —— 同一套 DataOps
#: 后端，白名单就该是同一套。
_SAFE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 分区过滤条件：``列名 = 'YYYYMMDD'``（天分区 ``pt_d``）。
#:
#: **只接受天分区**：小时/分钟分区（``pt_h`` / ``pt_i``）的过滤条件形状不同，
#: 且 DataOps 的分区裁剪对它们另有要求 —— 宁可拒绝让调用方显式处理，也不要
#: 拼一条跑不通的 SQL。
#:
#: 整串匹配（``^...$``）—— 前后不许有多余内容。允许列名与等号两侧有空格
#: （``pt_d='20261020'`` / ``pt_d = '20261020'`` 都认），但**只认**这一种
#: 形状。
_PARTITION_FILTER_RE = re.compile(
    r"^\s*(?P<col>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*'(?P<val>[^']*)'\s*$"
)


def is_safe_identifier(value: str) -> bool:
    """单段标识符是否合法（``[A-Za-z_][A-Za-z0-9_]*``）。

    与 :func:`is_safe_column_name` 同义 —— 保留两个名字是因为「列名」与
    「标识符」在调用点读起来更贴切（一个说列，一个说表名的一段）。
    """
    if not isinstance(value, str) or not value.strip():
        return False
    return bool(_SAFE_ID_RE.match(value.strip()))


def is_safe_column_name(value: str) -> bool:
    """列名是否合法 —— **只接受单段标识符**。

    不接受 ``db.tbl.col``（带库表前缀）与 ``*``：

    - 前缀会被反引号原样包成 `` `db.tbl.col` ``，后端解析不出来；
    - ``*`` 是 DataOps 平台**硬性禁止**的（且会让「按字段统计覆盖率」失去意义）。
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped or "." in stripped or "*" in stripped:
        return False
    return bool(_SAFE_ID_RE.match(stripped))


def is_safe_table_name(value: str) -> bool:
    """表名是否合法 —— **多段**，每段都要是合法标识符。

    拒绝空段（``"db."`` / ``".tbl"``）—— 否则拼出来的
    ``` `db`.`` / ``` `.`tbl` ``` 是语法错误。
    """
    if not isinstance(value, str) or not value.strip():
        return False
    parts = value.strip().split(".")
    if any(not part for part in parts):
        return False
    return all(_SAFE_ID_RE.match(part) for part in parts)


def quote_column(name: str) -> str:
    """给列名加反引号（已加的不重复加）。

    Raises:
        UnsafeIdentifierError: 列名没通过白名单。
    """
    if not is_safe_column_name(name):
        raise UnsafeIdentifierError(
            f"非法列名 {name!r} —— 列名只允许单段 [A-Za-z_][A-Za-z0-9_]*，"
            "不能带库/表前缀（如 'db.tbl.col'），也不能是 '*'。"
        )
    return f"`{name.strip()}`"


def quote_table(name: str) -> str:
    """给表名按段加反引号（``db.tbl`` → ``` `db`.`tbl` ```）。

    Raises:
        UnsafeIdentifierError: 表名没通过白名单。
    """
    if not is_safe_table_name(name):
        raise UnsafeIdentifierError(
            f"非法表名 {name!r} —— 表名只允许 [A-Za-z_][A-Za-z0-9_]*，"
            "多段时用 '.' 分隔（如 'db.tbl'），每段都必须是合法标识符。"
        )
    return ".".join(f"`{part}`" for part in name.strip().split("."))


def validate_partition_filter(value: str) -> str:
    """校验分区过滤条件并返回归一化后的整串。

    只接受「列名 = '字面量'」这一种形状（见 :data:`_PARTITION_FILTER_RE`）。
    空串**直接放行** —— 调用方用空串表达「明确不过滤」，那是合法的三态语义
    （见 ``CheckContext.partition_filter``）。

    Raises:
        UnsafeIdentifierError: 条件形状不合法。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise UnsafeIdentifierError(
            f"partition_filter 必须是字符串，实际是 {type(value).__name__}。"
        )
    text = value.strip()
    if not text:
        return ""

    m = _PARTITION_FILTER_RE.match(text)
    if not m:
        raise UnsafeIdentifierError(
            f"partition_filter={value!r} 不符合白名单 —— 只接受 '列名 = \'字面量\'' "
            "这一种形状（如 pt_d = '20261020'）；不接受函数调用、占位符"
            "（$date 等）、多条件拼接或分号。"
        )
    # 字面量再做一层收紧：只允许日期样的数字串与常见的时间字面量。
    literal = m.group("val")
    if not literal or not re.fullmatch(r"[0-9A-Za-z_:+\-./ ]*", literal):
        raise UnsafeIdentifierError(
            f"partition_filter={value!r} 的字面量 {literal!r} 含非法字符。"
        )
    return f"{m.group('col')} = '{literal}'"
