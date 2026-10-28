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
"""数据正确性校验运行时（Data Quality Check）。

本模块把「空值覆盖率 / 单一值覆盖率 / 异常值占比」抽象成**可注册的校验器列表**。

**一个校验器 = 一条 SQL 语句**，语句内部用条件聚合**一次算完所有字段**
（不做「字段 × 校验项」的 UNION ALL 展开）。调用方按校验项逐条并发执行
（``asyncio.gather``），最后合并成一行指标。

**SQL 形状约束（生产 adHoc 踩过的坑）**：

1. **不能用 CTE**（``WITH _src AS (...)``）。生产 adHoc 的表名解析阶段会把
   ``FROM _src`` 当成**物理表名**，直接报
   ``table(s) agapdw._src do not exist in production environment``。
   采样源一律用**派生表**：``FROM (SELECT ... LIMIT N) AS _src``。
2. **``FROM`` 后面不能出现裸别名**。别名只能跟在 ``AS`` 后面；
   ``FROM _src`` 会被当成物理表，只有 ``FROM (...) AS _src`` 才安全。
3. **``LIMIT`` 必须落在派生表内部**。``SELECT COUNT(1) FROM t WHERE ... LIMIT N``
   的 ``LIMIT`` 作用在**聚合结果**上（只有 1 行），根本限制不了扫描量 ——
   采样是假的。只有放进派生表才真正只取前 N 行。

扩展方式（新增第 N 种校验）：在 :data:`_BUILTIN_CHECKERS` 追加一个
:class:`QualityChecker` 实例即可 —— SQL 生成、结果解析、阈值比对、
warning 文案都由 checker 自己描述，编排层无需改动。

对应 skill 文档：``de_agent/skill/dataops_quality_check/SKILL.md``。
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext

# Reused, do not redeclare. 导入方向固定为 validate_tool -> quality_check，
# 避免循环依赖（quality_check 不在模块级引用 validate_tool 的运行期状态）。
from dataagent.actions.tools.local_tool.dataops_validate_tool import (
    _extract_fields_from_insert_sql,
    _extract_insert_target_table,
    _get_dataops_env,
    _get_dataops_resource,
    _get_user_account,
    _is_insert_sql,
    _poll_until_done,
    partition_date_for_quality,
)
from dataagent.utils.log import logger

# ============================================================================
# Constants
# ============================================================================

# 校验临时表的默认分区列（与 dataops_validate_tool 的 INSERT 重写一致）
_DEFAULT_PARTITION_COL = "pt_d"
# 采样行数：只对临时表 LIMIT N 行做统计，避免全表扫描
_DEFAULT_SAMPLE_SIZE = 10000
# 校验 SQL 自身的超时上限（秒）
#
# 质量校验的 SQL 走的是 Spark adHoc，与主 INSERT 同一个执行引擎，但聚合了
# 多个指标的统计（count/sum(if(...))/count(1) 等），在千万级分区上单条就能跑
# 十几分钟。与主链路对齐到 30 分钟，让慢 SQL 有机会跑完。
_MAX_CHECK_TIMEOUT_SEC = 30 * 60
_MAX_MAIN_TIMEOUT_SEC = 30 * 60

# 默认排除字段：分区列 / 时间戳 / 主键 ID —— 这些字段做覆盖率统计没有业务含义
DEFAULT_EXCLUDE_FIELDS = frozenset({
    "pt_d",
    "pt",
    "pt_h",
    "create_time",
    "update_time",
    "modify_time",
    "id",
    "uuid",
    "udid",
    "guid",
    "row_key",
})

_DEFAULT_NULL_RATE_THRESHOLD = 0.80
_DEFAULT_SINGLE_VALUE_THRESHOLD = 0.95
# 异常值占比阈值：参考值 0 —— 只要出现一个 <0 或 >1 的比率取值即告警。
_DEFAULT_OUTLIER_RATIO_THRESHOLD = 0.0
# 比率类字段的默认识别规则：字段名包含 "ratio"。
# 可通过 tool_config / resource metadata 的 ratio_fields、ratio_field_patterns 覆盖。
_DEFAULT_RATIO_FIELD_PATTERNS: tuple[str, ...] = ("ratio",)

# 环境变量开关沿用历史命名，向后兼容
_ENABLE_VALUES = frozenset({"true", "1", "yes", "on", "y", "t"})


# ============================================================================
# Checker abstraction
# ============================================================================


@dataclass(frozen=True)
class CheckContext:
    """一次质量校验所需的全部输入（checker 生成 SQL 时的唯一数据来源）。"""

    #: 校验 SQL 直接读取的表。两种模式：
    #:   - 临时表模式（交付后 post-hook）：``adhoctemp.tmp_*``（试算写入的表）
    #:   - 直连模式（独立数据验证 skill）：INSERT 的**真实目标表** ``db.table``
    temp_table: str
    fields: list[str]
    #: 本次实际生效的 checker；显式携带，避免模块级可变状态（并发安全）。
    checkers: tuple[QualityChecker, ...]
    partition_col: str = _DEFAULT_PARTITION_COL
    partition_date: str = ""
    #: 显式的分区过滤条件（形如 ``pt_d = '20261020'``）。非空时优先于
    #: ``partition_col`` + ``partition_date`` 拼出来的条件 —— 直连真实目标表
    #: 时用它限定「查哪个分区」。
    partition_filter_expr: str | None = None
    sample_size: int = _DEFAULT_SAMPLE_SIZE
    #: 显式指定的比率类字段（优先于模式匹配）。
    ratio_fields: frozenset[str] = frozenset()
    #: 比率类字段名模式（小写子串匹配）。
    ratio_field_patterns: tuple[str, ...] = _DEFAULT_RATIO_FIELD_PATTERNS
    #: 字段类型（来自交付的 DDL），小写，如 ``{"udid_sha256": "string"}``。
    #: 用来区分 STRING（用 ``bicoredata.IsEmpty`` 判空）与数值类型（用 ``IS NOT NULL``）。
    column_types: dict[str, str] = field(default_factory=dict)

    def column(self, field_name: str) -> str:
        """反引号包裹列名（已包裹的不重复包裹）。"""
        return field_name if field_name.startswith("`") else f"`{field_name}`"

    def is_string_field(self, field_name: str) -> bool:
        """字段是否按 **STRING** 存储。

        STRING 存储的字段判空必须用 ``bicoredata.IsEmpty``（空字符串也算空），
        数值/时间戳等非字符串存储则用 ``IS NOT NULL``。
        类型未知（没拿到 DDL）时按非字符串处理 —— ``IS NOT NULL`` 对所有类型都成立，
        比误用 IsEmpty 安全。
        """
        declared = self.column_types.get(field_name, "")
        return declared.startswith("string")

    def is_ratio_field(self, field_name: str) -> bool:
        """判断字段是否属于「比率类」（只对这些字段做异常值占比校验）。

        判定顺序：
        1. 配置了 ``ratio_fields`` → **以显式清单为准**（清单外的字段一律不算比率类）；
        2. 未配置 ``ratio_fields`` → 字段名（小写）包含任一 ``ratio_field_patterns``
           模式即算比率类。
        """
        if self.ratio_fields:
            return field_name in self.ratio_fields
        lowered = field_name.lower()
        return any(pattern and pattern.lower() in lowered for pattern in self.ratio_field_patterns)

    def partition_filter(self) -> str:
        """分区过滤条件。

        显式给的 ``partition_filter_expr`` 优先（直连真实目标表时用）；
        否则用 ``partition_col`` + ``partition_date`` 拼（临时表模式 ——
        临时表只写了一个分区）。
        """
        explicit = (self.partition_filter_expr or "").strip()
        if explicit:
            return explicit
        return f"{self.partition_col} = '{self.partition_date}'"


@dataclass(frozen=True)
class QualityChecker:
    """单个校验项的描述。

    一个 checker 负责三件事：**生成自己那条校验 SQL**（一次算完它负责的所有字段）、
    判定是否违规、生成 warning 文案。编排层按 checker **逐条执行** SQL 再把结果
    合并 —— 绝不把「checker × 字段」拼成一条 UNION ALL 大查询。

    SQL 形状对齐业务侧手写的示例口径（一条 checker 一条语句）::

        空值率      SUM(IF(字段有值, 1, 0)) / COUNT(1)
        单一值比率  max(各值出现次数) / 总样本数
        异常值占比  SUM(IF(比率 < 0 OR 比率 > 1, 1, 0)) / COUNT(1)
    """

    name: str
    #: 写进 details[field] 的 key，例如 "null_rate"
    metric_key: str
    #: 配置项里的阈值键名，例如 "null_rate_threshold"
    threshold_key: str
    default_threshold: float
    #: 生成这条 checker 的完整 SQL 语句（一次算完所有适用字段）。
    build_sql: Callable[[CheckContext], str]
    #: (实测值, 阈值) -> 是否违规
    violates: Callable[[float, float], bool]
    #: 生成人类可读 warning
    format_warning: Callable[[str, float, float], str]
    #: (字段名, 配置) -> 该 checker 是否适用于该字段；``None`` 表示对所有字段适用。
    #: 用于「只对某一类字段生效」的校验（如异常值占比只针对比率类字段）。
    applies_to: Callable[[str, QualityCheckConfig], bool] | None = None

    def applies_to_field(self, field_name: str, config: QualityCheckConfig) -> bool:
        """该 checker 是否适用于给定字段。"""
        if self.applies_to is None:
            return True
        return bool(self.applies_to(field_name, config))

    def alias(self, field_name: str) -> str:
        """该 checker 在**旧格式**结果行中的列别名（仅兼容旧解析函数）。"""
        return f"{self.metric_key}__{field_name}"


# ---------------------------------------------------------------------------
# SQL 生成辅助
# ---------------------------------------------------------------------------


def _sample_source(ctx: CheckContext, columns: list[str], alias: str = "_src") -> str:
    """采样源 —— **派生表**，不是 CTE。

    形状::

        (SELECT col1, col2 FROM t WHERE pt_d = '...' LIMIT 10000) AS _src

    **为什么不能用 ``WITH _src AS (...)``**：生产 adHoc 引擎不认 CTE ——
    解析阶段会把 ``FROM _src`` 当成**物理表名**去解析，于是报
    ``table(s) agapdw._src do not exist in production environment``。
    派生表是标准 SQL，生产/联调两条链路都支持。

    **为什么必须把 ``LIMIT`` 放进派生表**：``SELECT COUNT(1) FROM t WHERE ... LIMIT 10000``
    里的 ``LIMIT`` 作用在**聚合结果**上（只有 1 行），根本限制不了扫描量 ——
    全表照样扫。放进派生表里才真正只取前 N 行。

    所有指标都基于**同一批采样行**计算；``sample_size <= 0`` 时不采样（全量扫描）。
    """
    cols = ", ".join(ctx.column(c) for c in columns)
    limit = f"\n  LIMIT {ctx.sample_size}" if ctx.sample_size > 0 else ""
    return (
        "(\n"
        f"  SELECT {cols}\n"
        f"  FROM {ctx.temp_table}\n"
        f"  WHERE {ctx.partition_filter()}{limit}\n"
        f") AS {alias}"
    )


def _wrap_sample(ctx: CheckContext, metrics: list[str], columns: list[str]) -> str:
    """把指标表达式包成一条完整可执行的 SQL（采样源 + SELECT）。"""
    select_list = ",\n  ".join(metrics)
    return f"SELECT\n  {select_list}\nFROM {_sample_source(ctx, columns)}"


# ---------------------------------------------------------------------------
# 内置 checker：空值覆盖率
# ---------------------------------------------------------------------------


def _build_null_rate_sql(ctx: CheckContext) -> str:
    """空值率（覆盖率）SQL —— 一条语句算完所有字段。

    口径（业务示例口径）::

        SUM(IF(字段有值, 1, 0)) / COUNT(1)

    形状对齐业务侧手写示例::

        SELECT
          SUM(IF(!bicoredata.IsEmpty(udid), 1, 0)) / COUNT(1) AS udid
        FROM <临时表>
        WHERE pt_d = '<date>'

    STRING 存储的字段用 ``!bicoredata.IsEmpty`` 判空（空字符串也算空）；
    数值等非字符串存储用 ``IS NOT NULL``（``bicoredata.IsEmpty`` 只适用于字符串）。

    采样放在**派生表**里（``LIMIT`` 必须在扫描前生效，见 :func:`_sample_source`），
    所以外层 FROM 指向的是派生表而不是物理表。
    """
    if not ctx.fields:
        return ""
    metrics: list[str] = []
    for name in ctx.fields:
        col = ctx.column(name)
        condition = (
            f"!bicoredata.IsEmpty({col})"
            if ctx.is_string_field(name)
            else f"{col} IS NOT NULL"
        )
        metrics.append(f"SUM(IF({condition}, 1, 0)) / COUNT(1) AS {name}")
    return _wrap_sample(ctx, metrics, columns=ctx.fields)


def _null_rate_violates(value: float, threshold: float) -> bool:
    # 统计的是"非空率"，低于阈值即违规
    return value < threshold


def _null_rate_warning(field_name: str, value: float, threshold: float) -> str:
    return (
        f"字段 '{field_name}' 非空率 {value:.4f} < 阈值 {threshold:.2f}"
        f"（空值率 {(1 - value):.4f}）"
    )


NULL_RATE_CHECKER = QualityChecker(
    name="null_rate",
    metric_key="null_rate",
    threshold_key="null_rate_threshold",
    default_threshold=_DEFAULT_NULL_RATE_THRESHOLD,
    build_sql=_build_null_rate_sql,
    violates=_null_rate_violates,
    format_warning=_null_rate_warning,
)


# ---------------------------------------------------------------------------
# 内置 checker：单一值覆盖率
# ---------------------------------------------------------------------------


def _build_single_value_ratio_sql(ctx: CheckContext) -> str:
    """单一值比率 SQL —— 每个字段一个标量子查询。

    口径（业务示例口径）::

        max(各值出现次数) / 总样本数

    形状对齐业务侧手写示例::

        ROUND((SELECT MAX(cnt) FROM (SELECT COUNT(*) AS cnt FROM <采样> GROUP BY col) AS t)
              / COUNT(1), 4) AS col

    **采样源要内联在每个字段的子查询里**，不能引用外层别名 ——
    生产 adHoc 的表名解析会把 ``FROM <别名>`` 当成物理表去找（见
    :func:`_sample_source` 的说明），别名只能出现在 ``AS`` 后面。
    """
    if not ctx.fields:
        return ""
    # 每个字段的子查询用**同一份**采样（同样的列、同样的 LIMIT），
    # 这样分子（MAX(cnt)）和分母（外层 COUNT(1)）落在同一批行上。
    sample = _sample_source(ctx, ctx.fields, alias="_s")
    metrics: list[str] = []
    for name in ctx.fields:
        col = ctx.column(name)
        metrics.append(
            f"ROUND((SELECT MAX(cnt) FROM (SELECT COUNT(*) AS cnt FROM "
            f"{sample} GROUP BY {col}) AS t) / COUNT(1), 4) AS {name}"
        )
    return _wrap_sample(ctx, metrics, columns=ctx.fields)


def _single_value_violates(value: float, threshold: float) -> bool:
    return value > threshold


def _single_value_warning(field_name: str, value: float, threshold: float) -> str:
    return f"字段 '{field_name}' 单一值比率 {value:.4f} > 阈值 {threshold:.2f}"


SINGLE_VALUE_CHECKER = QualityChecker(
    name="single_value_ratio",
    metric_key="single_value_ratio",
    threshold_key="single_value_threshold",
    default_threshold=_DEFAULT_SINGLE_VALUE_THRESHOLD,
    build_sql=_build_single_value_ratio_sql,
    violates=_single_value_violates,
    format_warning=_single_value_warning,
)


# ---------------------------------------------------------------------------
# 内置 checker：异常值占比（仅应用于比率类原始值）
# ---------------------------------------------------------------------------
#
# 业务口径（对齐需求文档 1.3）：
#     异常值占比 = SUM(比率 < 0 OR 比率 > 1) / 样本量
#     参考值 = 0  —— 只要出现一个越界取值即告警。
#
# 只对「比率类」字段生效。比率类的判定：
#     1. 字段被显式列入配置 ratio_fields；或
#     2. 字段名（小写）包含任一 ratio_field_patterns 模式（默认 "ratio"）。
# 非比率类字段不出现在这条 SQL 里，也不参与告警。


def _build_outlier_ratio_sql(ctx: CheckContext) -> str:
    """异常值占比 SQL —— 只对比率类字段。

    口径（业务示例口径）::

        SUM(IF(比率 < 0 OR 比率 > 1, 1, 0)) / COUNT(1)
    """
    ratio_fields = [name for name in ctx.fields if ctx.is_ratio_field(name)]
    if not ratio_fields:
        return ""
    metrics: list[str] = []
    for name in ratio_fields:
        col = ctx.column(name)
        metrics.append(
            f"SUM(IF(({col} < 0 OR {col} > 1), 1, 0)) / COUNT(1) AS {name}"
        )
    return _wrap_sample(ctx, metrics, columns=ratio_fields)


def _outlier_ratio_violates(value: float, threshold: float) -> bool:
    # 异常值占比 > 阈值即违规（参考值 0 → 只要有越界取值就告警）。
    return value > threshold


def _outlier_ratio_warning(field_name: str, value: float, threshold: float) -> str:
    return (
        f"字段 '{field_name}' 异常值占比 {value:.4f} > 阈值 {threshold:.2f}"
        f"（存在 <0 或 >1 的比率取值）"
    )


OUTLIER_RATIO_CHECKER = QualityChecker(
    name="outlier_ratio",
    metric_key="outlier_ratio",
    threshold_key="outlier_ratio_threshold",
    default_threshold=_DEFAULT_OUTLIER_RATIO_THRESHOLD,
    build_sql=_build_outlier_ratio_sql,
    violates=_outlier_ratio_violates,
    format_warning=_outlier_ratio_warning,
    applies_to=lambda field_name, config: config.is_ratio_field(field_name),
)


# 全部内置 checker —— 新增校验只需在这里追加一项
_BUILTIN_CHECKERS: tuple[QualityChecker, ...] = (
    NULL_RATE_CHECKER,
    SINGLE_VALUE_CHECKER,
    OUTLIER_RATIO_CHECKER,
)

_BUILTIN_BY_NAME: dict[str, QualityChecker] = {c.name: c for c in _BUILTIN_CHECKERS}


def select_checkers(names: Sequence[str] | None) -> tuple[QualityChecker, ...]:
    """按名称挑出生效 checker；``None`` / 空表示全部内置。"""
    if not names:
        return _BUILTIN_CHECKERS
    selected = tuple(_BUILTIN_BY_NAME[n] for n in names if n in _BUILTIN_BY_NAME)
    return selected or _BUILTIN_CHECKERS


# ============================================================================
# DDL parsing — column types
#
# 判空方式取决于**存储类型**：
#   STRING  → ``!bicoredata.IsEmpty(col)``（空字符串也算空）
#   数值/时间戳等 → ``col IS NOT NULL``
# 所以要尽量从交付的 DDL 里读出真实类型。读不到就退回 ``IS NOT NULL``
# （对所有类型都成立），宁可少用 IsEmpty 也不能给数值列套字符串判空。
# ============================================================================


def parse_ddl_column_types(ddl_sql: str) -> dict[str, str]:
    """从 Hive DDL 里解析 ``{列名: 小写类型}``。

    支持 ``CREATE EXTERNAL TABLE ... ( col STRING COMMENT '...' , ... )``，
    忽略 ``PARTITIONED BY`` 里的分区列（分区列不进业务字段）。
    """
    if not ddl_sql:
        return {}
    # 去掉注释，避免注释里的括号干扰列清单切分。
    text = re.sub(r"/\*.*?\*/", " ", ddl_sql, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)

    open_m = re.search(r"\((?!\s*PARTITIONED\s+BY\b)", text, re.IGNORECASE)
    if not open_m:
        return {}
    # 找到与该左括号配对的右括号。
    depth = 0
    close_idx = -1
    for i in range(open_m.end() - 1, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                close_idx = i
                break
    if close_idx < 0:
        return {}

    body = text[open_m.end() : close_idx]
    column_types: dict[str, str] = {}
    for part in _split_top_level_commas(body):
        part = part.strip()
        if not part:
            continue
        # ``col TYPE [COMMENT '...']`` —— 类型名是第一个 token。
        m = re.match(r"`?([A-Za-z_][A-Za-z0-9_]*)`?\s+([A-Za-z_][A-Za-z0-9_]*)", part)
        if not m:
            continue
        column_types[m.group(1)] = m.group(2).lower()
    return column_types


def _split_top_level_commas(text: str) -> list[str]:
    """按**顶层**逗号切分（括号内、字符串里的逗号不算分隔符）。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in text:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _find_ddl_in_workspace(runtime: Any) -> str:
    """在 workspace 里找交付的 ``create_*.sql``（取第一个）。"""
    raw = getattr(runtime, "workspace_dir", None) if runtime is not None else None
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        return ""
    try:
        workspace = Path(raw).expanduser().resolve()
    except OSError:
        return ""
    if not workspace.is_dir():
        return ""
    for directory in (workspace / "sql", workspace):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            name = path.name.lower()
            if name.startswith("create_") and name.endswith(".sql") and path.is_file():
                try:
                    return path.read_text(encoding="utf-8")
                except OSError:
                    return ""
    return ""


# ============================================================================
# Configuration
# ============================================================================


@dataclass
class QualityCheckConfig:
    """一次质量校验使用的解析后配置。"""

    enabled: bool = True
    sample_size: int = _DEFAULT_SAMPLE_SIZE
    partition_col: str = _DEFAULT_PARTITION_COL
    exclude_fields: frozenset[str] = field(default_factory=lambda: DEFAULT_EXCLUDE_FIELDS)
    checker_names: tuple[str, ...] = tuple(c.name for c in _BUILTIN_CHECKERS)
    thresholds: dict[str, float] = field(default_factory=dict)
    #: 显式声明的比率类字段（异常值占比校验只对这些字段生效）。
    ratio_fields: frozenset[str] = frozenset()
    #: 比率类字段名模式（小写子串匹配）；为空表示不做模式匹配。
    ratio_field_patterns: tuple[str, ...] = _DEFAULT_RATIO_FIELD_PATTERNS

    @property
    def checkers(self) -> tuple[QualityChecker, ...]:
        """本次实际生效的 checker 实例列表。"""
        return select_checkers(self.checker_names)

    def threshold_for(self, checker: QualityChecker) -> float:
        return self.thresholds.get(checker.threshold_key, checker.default_threshold)

    def is_ratio_field(self, field_name: str) -> bool:
        """判断字段是否属于「比率类」（与 :meth:`CheckContext.is_ratio_field` 同规则）。"""
        if self.ratio_fields:
            return field_name in self.ratio_fields
        lowered = field_name.lower()
        return any(pattern and pattern.lower() in lowered for pattern in self.ratio_field_patterns)


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if not text:
        return default
    return text in _ENABLE_VALUES


def _as_float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _as_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _resource_quality_check_metadata(_tool_context: ToolExecutionContext | None) -> dict[str, Any]:
    """从 dataops resource metadata 读取 quality_check 配置段。"""
    try:
        runtime = _tool_context.runtime if _tool_context is not None else None
        if runtime is None:
            return {}
        resource = _get_dataops_resource(runtime)
        if resource is None:
            return {}
        raw = (resource.metadata or {}).get("quality_check") or {}
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[quality_check] resource metadata unreadable: {exc}")
        return {}


def _pick(sections: tuple[dict[str, Any], ...], key: str) -> Any:
    """按 ``tool_config > resource metadata`` 顺序取第一个出现的值。"""
    for source in sections:
        candidate = source.get(key)
        if candidate is not None:
            return candidate
    return None


def resolve_config(_tool_context: ToolExecutionContext | None = None) -> QualityCheckConfig:
    """按 ``tool_config > resource metadata > 环境变量 > 默认值`` 解析配置。

    Args:
        _tool_context: 工具执行上下文；``None`` 时只用环境变量 + 默认值。
    """
    tool_cfg: dict[str, Any] = {}
    if _tool_context is not None:
        raw_cfg = getattr(_tool_context, "tool_config", None)
        if isinstance(raw_cfg, dict):
            nested = raw_cfg.get("quality_check")
            tool_cfg = nested if isinstance(nested, dict) else raw_cfg

    resource_cfg = _resource_quality_check_metadata(_tool_context)

    return _resolve_config_locked(tool_cfg, resource_cfg)


def _resolve_config_locked(tool_cfg: dict[str, Any], resource_cfg: dict[str, Any]) -> QualityCheckConfig:
    # 配置来源链，按优先级排列（显式传参，不使用模块级可变状态 —— 并发安全）。
    sections: tuple[dict[str, Any], ...] = (tool_cfg, resource_cfg)

    # --- 阈值（支持 {"thresholds": {...}} 嵌套与扁平两种写法） ---
    raw_thresholds: dict[str, Any] = {}
    for source in (tool_cfg, resource_cfg):
        candidate = source.get("thresholds")
        if isinstance(candidate, dict):
            raw_thresholds.update(candidate)
    for source in (tool_cfg, resource_cfg):
        for key in ("null_rate_threshold", "single_value_threshold", "outlier_ratio_threshold"):
            if key in source:
                raw_thresholds[key] = source[key]

    thresholds = {c.threshold_key: c.default_threshold for c in _BUILTIN_CHECKERS}
    for checker in _BUILTIN_CHECKERS:
        for key in (checker.metric_key, checker.threshold_key):
            if key in raw_thresholds:
                thresholds[checker.threshold_key] = _as_float(
                    raw_thresholds[key], checker.default_threshold
                )

    # --- 启用开关 ---
    enabled = _as_bool(_pick(sections, "enabled"), True)
    if not tool_cfg and not resource_cfg:
        # 无任何 YAML 配置时，用环境变量兜底（历史命名，保持兼容）
        enabled = _as_bool(os.environ.get("DATAOPS_ENABLE_REASONABLE_CHECK"), True)

    # --- 采样 / 分区列 ---
    sample_size = _as_int(_pick(sections, "sample_size"), _DEFAULT_SAMPLE_SIZE)
    partition_col = str(_pick(sections, "partition_col") or _DEFAULT_PARTITION_COL)

    # --- 排除字段 ---
    exclude_raw = _pick(sections, "exclude_fields")
    exclude_fields = (
        frozenset(str(x) for x in exclude_raw)
        if isinstance(exclude_raw, (list, tuple, set))
        else DEFAULT_EXCLUDE_FIELDS
    )

    # --- 生效 checker ---
    checks_raw = _pick(sections, "checks")
    if not isinstance(checks_raw, (list, tuple)):
        env_checks = os.environ.get("DATAOPS_QUALITY_CHECK_CHECKS", "").strip()
        checks_raw = [c.strip() for c in env_checks.split(",") if c.strip()] if env_checks else None
    checker_names = (
        tuple(str(n) for n in checks_raw)
        if isinstance(checks_raw, (list, tuple))
        else tuple(c.name for c in _BUILTIN_CHECKERS)
    )

    # --- 比率类字段识别（异常值占比校验的适用范围） ---
    ratio_fields_raw = _pick(sections, "ratio_fields")
    ratio_fields = (
        frozenset(str(x) for x in ratio_fields_raw)
        if isinstance(ratio_fields_raw, (list, tuple, set))
        else frozenset()
    )
    patterns_raw = _pick(sections, "ratio_field_patterns")
    ratio_field_patterns = (
        tuple(str(p) for p in patterns_raw)
        if isinstance(patterns_raw, (list, tuple))
        else _DEFAULT_RATIO_FIELD_PATTERNS
    )

    return QualityCheckConfig(
        enabled=enabled,
        sample_size=sample_size,
        partition_col=partition_col,
        exclude_fields=exclude_fields,
        checker_names=checker_names,
        thresholds=thresholds,
        ratio_fields=ratio_fields,
        ratio_field_patterns=ratio_field_patterns,
    )


# ============================================================================
# SQL generation
# ============================================================================


def build_check_context(
    temp_table: str,
    fields: list[str],
    partition_col: str = _DEFAULT_PARTITION_COL,
    partition_date: str = "",
    sample_size: int = _DEFAULT_SAMPLE_SIZE,
    checkers: Sequence[QualityChecker] | None = None,
    ratio_fields: frozenset[str] | None = None,
    ratio_field_patterns: tuple[str, ...] | None = None,
    column_types: dict[str, str] | None = None,
    partition_filter: str | None = None,
) -> CheckContext:
    """组装 checker 生成 SQL 所需的上下文。"""
    active = tuple(checkers) if checkers is not None else _BUILTIN_CHECKERS
    return CheckContext(
        temp_table=temp_table,
        fields=list(fields),
        checkers=active,
        partition_col=partition_col,
        partition_date=partition_date,
        sample_size=sample_size,
        ratio_fields=ratio_fields or frozenset(),
        ratio_field_patterns=(
            ratio_field_patterns
            if ratio_field_patterns is not None
            else _DEFAULT_RATIO_FIELD_PATTERNS
        ),
        column_types=dict(column_types or {}),
        partition_filter_expr=partition_filter,
    )


def build_quality_check_sqls(
    temp_table: str,
    fields: list[str],
    partition_col: str = _DEFAULT_PARTITION_COL,
    partition_date: str = "",
    sample_size: int = _DEFAULT_SAMPLE_SIZE,
    checkers: Sequence[QualityChecker] | None = None,
    ratio_fields: frozenset[str] | None = None,
    ratio_field_patterns: tuple[str, ...] | None = None,
    column_types: dict[str, str] | None = None,
    partition_filter: str | None = None,
) -> list[tuple[str, str]]:
    """生成**每个 checker 一条**校验 SQL。

    Returns:
        ``[(checker_name, sql), ...]`` —— 逐条执行即可。

    每个 checker 的 SQL 内部用条件聚合一次算完它负责的所有字段，
    **不做**「checker × 字段」的 UNION ALL 展开。

    ``partition_filter`` 显式给出分区过滤条件（形如 ``pt_d = '20261020'``），
    用于**直连真实目标表**的模式；为 ``None`` 时用 ``partition_col`` +
    ``partition_date`` 拼（临时表模式，临时表只写了一个分区）。
    """
    if not fields:
        return []
    ctx = build_check_context(
        temp_table=temp_table,
        fields=fields,
        partition_col=partition_col,
        partition_date=partition_date,
        sample_size=sample_size,
        checkers=checkers,
        ratio_fields=ratio_fields,
        ratio_field_patterns=ratio_field_patterns,
        column_types=column_types,
        partition_filter=partition_filter,
    )
    statements: list[tuple[str, str]] = []
    for checker in ctx.checkers:
        sql = checker.build_sql(ctx)
        if sql:
            statements.append((checker.name, sql))
    return statements


def build_quality_check_sql(
    temp_table: str,
    fields: list[str],
    partition_col: str = _DEFAULT_PARTITION_COL,
    partition_date: str = "",
    sample_size: int = _DEFAULT_SAMPLE_SIZE,
    checkers: Sequence[QualityChecker] | None = None,
    ratio_fields: frozenset[str] | None = None,
    ratio_field_patterns: tuple[str, ...] | None = None,
    column_types: dict[str, str] | None = None,
) -> str:
    """Deprecated: 用 :func:`build_quality_check_sqls`（一个 checker 一条 SQL）。

    保留这个函数只是为了兼容旧调用方；它现在返回**第一条** checker 的 SQL，
    不再拼 UNION ALL 大查询。生产代码请用 :func:`build_quality_check_sqls`。
    """
    statements = build_quality_check_sqls(
        temp_table=temp_table,
        fields=fields,
        partition_col=partition_col,
        partition_date=partition_date,
        sample_size=sample_size,
        checkers=checkers,
        ratio_fields=ratio_fields,
        ratio_field_patterns=ratio_field_patterns,
        column_types=column_types,
    )
    return statements[0][1] if statements else ""


# ============================================================================
# Result parsing
# ============================================================================


def _coerce_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_checker_result(
    result: dict[str, Any],
    checker: QualityChecker,
    fields: list[str],
    config: QualityCheckConfig | None = None,
) -> dict[str, Any]:
    """解析**单个 checker** 的结果行。

    新 SQL 里每个 checker 的结果行就是 ``{字段名: 指标值}``（列名 = 字段名），
    所以这里按字段名取值；旧的 ``null_rate__col1`` 形式也一并兼容。

    ``details[field][checker.metric_key]`` —— 与历史返回结构保持一致。
    """
    cfg = config or resolve_config(None)

    row: dict[str, Any] = {}
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        row = data[0]

    details: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for field_name in fields:
        if not checker.applies_to_field(field_name, cfg):
            # 该 checker 不适用于此字段（如异常值占比只针对比率类字段）。
            details[field_name] = {checker.metric_key: None}
            continue
        value = _coerce_float(row.get(field_name))
        if value is None:
            # 兼容旧格式的行（列名带 checker 前缀）。
            value = _coerce_float(row.get(checker.alias(field_name)))
        details[field_name] = {checker.metric_key: value}
        if value is None:
            continue
        threshold = cfg.threshold_for(checker)
        if checker.violates(value, threshold):
            warnings.append(checker.format_warning(field_name, value, threshold))

    return {"ok": len(warnings) == 0, "warnings": warnings, "details": details}


def parse_quality_check_result(
    result: dict[str, Any],
    fields: list[str],
    config: QualityCheckConfig | None = None,
    checkers: Sequence[QualityChecker] | None = None,
) -> dict[str, Any]:
    """把**旧格式**（列名带 checker 前缀）的结果行拆成 ``{ok, warnings, details}``。

    ``details[field][checker.metric_key]`` —— 与历史返回结构保持一致。
    新代码请用 :func:`parse_checker_result`（每个 checker 单独解析一行）。
    """
    cfg = config or resolve_config(None)
    active = tuple(checkers) if checkers is not None else cfg.checkers

    row: dict[str, Any] = {}
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        row = data[0]

    details: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for field_name in fields:
        per_field: dict[str, Any] = {}
        for checker in active:
            if not checker.applies_to_field(field_name, cfg):
                # 该 checker 不适用于此字段（如异常值占比只针对比率类字段）。
                per_field[checker.metric_key] = None
                continue
            value = _coerce_float(row.get(checker.alias(field_name)))
            per_field[checker.metric_key] = value
            if value is None:
                continue
            threshold = cfg.threshold_for(checker)
            if checker.violates(value, threshold):
                warnings.append(checker.format_warning(field_name, value, threshold))
        details[field_name] = per_field

    return {"ok": len(warnings) == 0, "warnings": warnings, "details": details}


def merge_checker_results(
    parsed_results: Sequence[tuple[str, dict[str, Any]]],
    fields: list[str],
    config: QualityCheckConfig | None = None,
    checkers: Sequence[QualityChecker] | None = None,
) -> dict[str, Any]:
    """把多个 checker 的解析结果合并成一份 ``{ok, warnings, details}``。

    每个 checker 一条 SQL → 一份解析结果；这里按字段把各 checker 的指标
    拼到同一个 ``details[field]`` 里，warnings 顺次累加。

    ``details`` 的形状是**稳定的**：每个字段都带上全部生效 checker 的 metric key。
    某个 checker 没跑（比如没有比率类字段，异常值占比那条 SQL 压根没生成）
    或该字段不适用时，对应的值是 ``None`` —— 而不是缺这个 key。
    """
    cfg = config or resolve_config(None)
    active = tuple(checkers) if checkers is not None else cfg.checkers
    by_name = {checker.name: checker for checker in active}

    details: dict[str, dict[str, Any]] = {
        name: {checker.metric_key: None for checker in active} for name in fields
    }
    warnings: list[str] = []

    for checker_name, parsed in parsed_results:
        checker = by_name.get(checker_name)
        metric_key = checker.metric_key if checker is not None else checker_name
        for field_name in fields:
            metrics = (parsed.get("details") or {}).get(field_name) or {}
            details.setdefault(field_name, {})[metric_key] = metrics.get(metric_key)
        warnings.extend(parsed.get("warnings") or [])

    return {"ok": len(warnings) == 0, "warnings": warnings, "details": details}


# ============================================================================
# Markdown rendering (前端展示)
# ============================================================================
#
# 「数据质量校验」的结果要送到前端。两条链路都走这里，保证**表格形状一致**：
#   1. 独立数据验证 skill（``deops_validate_data`` 工具）—— 直接返回表格；
#   2. 交付后 post-hook（``dataops_quality_check_post``）—— 追加一条 AIMessage。
#
# 列：字段名 / 非空率(覆盖率) / 单一值比率 / 异常值占比 / 状态。
# ``None`` 表示该指标**不适用**（非比率类字段不参与异常值占比；或该 checker
# 没生成对应 SQL）—— 渲染成 ``—``，而不是 0（0 会被误读成「真的算出是 0」）。


def render_metrics_table(
    details: dict[str, Any] | None,
    warnings: Sequence[str] | None = None,
    failed_metrics: dict[str, Sequence[str]] | None = None,
) -> str:
    """把质量校验的 ``details`` 渲染成 Markdown 表格（前端可直接渲染）。

    Args:
        details: ``{字段名: {metric_key: value | None}}``。``value`` 为 ``None``
            表示该指标不适用。
        warnings: 告警文案列表。用于给命中告警的字段标 ``⚠️ 告警``。
        failed_metrics: ``{字段名: [没算出来的 metric_key]}``。用于把「指标不适用」
            （渲染成 ``—``）和「指标没算出来」（渲染成 ``未完成``）区分开 —— 后者
            是执行失败/超时导致的缺口，不能读成「查过了，通过」。

    Returns:
        Markdown 表格文本；``details`` 为空时返回一句占位说明。
    """
    if not details:
        return "（无可展示的指标）"

    warned_fields = _warned_field_names(details, warnings or [])
    failed_by_field = {k: set(v or []) for k, v in (failed_metrics or {}).items()}
    lines = [
        "| 字段名 | 非空率(覆盖率) | 单一值比率 | 异常值占比 | 状态 |",
        "|---|---|---|---|---|",
    ]
    for field_name, metrics in details.items():
        metrics = metrics if isinstance(metrics, dict) else {}
        missing = failed_by_field.get(field_name, set())
        status = "⚠️ 告警" if field_name in warned_fields else "✅ 通过"
        # 该字段的所有指标都不适用（非比率类字段 / 对应 checker 没生成 SQL）
        # → 状态列不标「通过」，避免读成「查过了，没问题」。
        if not metrics or all(value is None for value in metrics.values()):
            status = "—"

        def _cell(metric_key: str, *, _field: str = field_name, _metrics: dict = metrics) -> str:
            """指标值；没算出来（执行失败/超时）渲染成「未完成」。"""
            if metric_key in failed_by_field.get(_field, ()):
                return "未完成"
            return format_ratio(_metrics.get(metric_key))

        # 该字段有指标没算出来 → 状态列标「部分未完成」，不能标「通过」。
        if missing and field_name not in warned_fields:
            status = "⚠️ 部分未完成"

        lines.append(
            f"| {field_name} "
            f"| {_cell('null_rate')} "
            f"| {_cell('single_value_ratio')} "
            f"| {_cell('outlier_ratio')} "
            f"| {status} |"
        )
    return "\n".join(lines)


def format_ratio(value: Any) -> str:
    """比率格式化：``None`` → ``—``，否则保留 4 位小数。"""
    if value is None:
        return "—"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "—"


def _warned_field_names(details: dict[str, Any], warnings: Sequence[str]) -> set[str]:
    """从告警文案里反推命中告警的字段名。

    告警文案由各 checker 自己生成，形如 ``字段 'col1' 非空率 0.4500 < 阈值 0.80``，
    所以用 ``'字段名'`` 的引号模式去匹配。
    """
    warned: set[str] = set()
    for warning in warnings:
        text = str(warning or "")
        for field_name in details:
            if f"'{field_name}'" in text:
                warned.add(field_name)
                break
    return warned


# ============================================================================
# Execution — prod (official MCP) / integration (resource-coordinator)
# ============================================================================


def _empty_result(
    job_id: str | None,
    sample_size: int,
    reason: str = "",
) -> dict[str, Any]:
    """校验**没能跑起来**时返回的结果。

    必须用 ``ok=False``：校验没执行 ≠ 数据没问题。用 ``sampled=False``
    表达「没采样」，``error`` 带上原因供排查。
    """
    result: dict[str, Any] = {
        "ok": False,
        "warnings": [],
        "details": {},
        "sampled": False,
        "sample_size": sample_size,
        "job_id": job_id,
    }
    if reason:
        result["error"] = reason
    return result


async def _execute_check_sql_prod(
    check_sql: str,
    timeout_sec: int,
) -> tuple[str, str | None, dict[str, Any] | None]:
    """生产路径：execute_sql → get_query_result → collect。"""
    from dataagent.actions.tools.local_tool.dataops_prod_adapter import (
        prod_collect_result,
        prod_execute_sql,
        prod_get_query_result,
    )

    try:
        submit_result = await prod_execute_sql(check_sql)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[quality_check] prod submit exception: {exc}")
        return "error", None, None
    if not submit_result.get("success") or not submit_result.get("job_id"):
        logger.debug(f"[quality_check] prod submit failed: {submit_result}")
        return "error", None, None
    job_id = submit_result["job_id"]

    deadline = asyncio.get_event_loop().time() + timeout_sec
    while asyncio.get_event_loop().time() < deadline:
        polled = await prod_get_query_result(job_id)
        status = polled.get("status")
        if status == "completed":
            return "completed", job_id, await prod_collect_result(job_id)
        if status in {"failed", "error"}:
            return "failed", job_id, None
        await asyncio.sleep(5.0)
    return "timed_out", job_id, None


async def _execute_check_sql_integration(
    check_sql: str,
    coordinator: Any,
    timeout_sec: int,
    poll_interval: float,
) -> tuple[str, str | None, dict[str, Any] | None]:
    """联调路径：submit_job → poll → collect。"""
    try:
        submit_result = coordinator.submit_job(
            resource_id="dataops",
            command=check_sql,
            task_type="sql_validate",
            timeout_sec=timeout_sec,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[quality_check] integration submit exception: {exc}")
        return "error", None, None

    job_id = submit_result.get("job_id")
    if submit_result.get("status") == "ERROR" or not job_id:
        logger.debug(f"[quality_check] integration submit failed: {submit_result}")
        return "error", None, None

    result_status, result = await _poll_until_done(coordinator, job_id, timeout_sec, poll_interval)
    if result_status != "completed" or not result:
        return result_status, job_id, None
    return "completed", job_id, result


# ============================================================================
# Public entry
# ============================================================================


def default_partition_date() -> str:
    """质量校验链路的分区日期（today - 7）。

    与「语法校验链路用一年之后」的日期刻意不同：本链路要读**真实产出**，
    一周前的分区已经稳定落盘。调用方（``trial_compute_table``）必须用同一个
    日期去试算 INSERT，否则临时表名与实际写入的表对不上。
    """
    return partition_date_for_quality()


async def run_quality_check(
    sql: str,
    *,
    _tool_context: ToolExecutionContext,
    config: QualityCheckConfig | None = None,
    temp_table: str | None = None,
    _partition_date: str | None = None,
    direct_table: str | None = None,
    partition_filter: str | None = None,
) -> dict[str, Any] | None:
    """对 INSERT 落地的表跑一轮数据正确性校验。

    **契约（两条链路共用，务必保持）：**
    - 只校验 INSERT 类 SQL；CREATE / SELECT / 空 SQL 一律返回 ``None``（跳过）。
    - 失败（阈值超标、执行报错、超时）都只体现在返回值里，**不抛异常**，
      **不会**让调用方的 ``passed`` 翻成 False。
    - 临时表模式下，调用方必须在临时表被 DROP **之前**调用本函数。

    **两种模式：**

    1. **临时表模式**（默认，交付后 post-hook 走这条）：校验试算写入的
       ``adhoctemp.tmp_*`` 临时表。``temp_table`` 为 ``None`` 时从 SQL 推导。
    2. **直连模式**（``direct_table`` 非空）：直接查 INSERT 的**真实目标表**，
       **不建表、不插入、不用临时表替换**。这是独立「数据验证」skill 走的路径。

    Args:
        sql: 原始 INSERT SQL（用于解析目标表与 SELECT 字段）。
        _tool_context: 工具执行上下文。
        config: 已解析配置；为 ``None`` 时按优先级重新解析。
        temp_table: 已知临时表名；为 ``None`` 时从 SQL 推导。
        _partition_date: 内部参数。已知分区日期；为 ``None`` 时用「一周之前」。
            必须与试算时写入临时表用的日期一致，否则会读到一张不存在的表。
        direct_table: 直连模式的目标表（``db.table``）。非空时**不走临时表**，
            直接对这张真实表做统计。
        partition_filter: 直连模式下的分区过滤条件（形如 ``pt_d = '20261020'``）。
            为 ``None`` 时用默认分区列 + 分区日期。

    Returns:
        ``None`` 表示跳过；否则返回
        ``{ok, warnings, details, sampled, sample_size, job_id, checks}``。
    """
    cfg = config or resolve_config(_tool_context)
    try:
        return await _run_quality_check_impl(
            sql,
            _tool_context=_tool_context,
            config=cfg,
            temp_table=temp_table,
            partition_date=_partition_date,
            direct_table=direct_table,
            partition_filter=partition_filter,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[quality_check] unexpected failure (ignored): {exc}")
        return _empty_result(None, cfg.sample_size, reason=f"unexpected failure: {exc}")


async def _run_quality_check_impl(
    sql: str,
    *,
    _tool_context: ToolExecutionContext,
    config: QualityCheckConfig,
    temp_table: str | None,
    partition_date: str | None,
    direct_table: str | None = None,
    partition_filter: str | None = None,
) -> dict[str, Any] | None:
    if not config.enabled:
        logger.debug("[quality_check] disabled by config")
        return None
    if not _is_insert_sql(sql or ""):
        return None

    runtime = _tool_context.runtime

    coordinator = None
    if _get_dataops_env() == "prod":
        user_account = _get_user_account(runtime) if runtime is not None else "anonymous"
        table_suffix = os.environ.get("DATAOPS_TABLE_SUFFIX", "")
        timeout_sec = _MAX_CHECK_TIMEOUT_SEC
        poll_interval = 5.0
    else:
        if runtime is None:
            logger.debug("[quality_check] skipped: no runtime")
            return None
        resource = _get_dataops_resource(runtime)
        if resource is None:
            return None
        try:
            coordinator = runtime.ensure_resource_coordinator()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[quality_check] skipped: no coordinator ({exc})")
            return None
        if coordinator is None:
            return None
        user_account = resource.metadata.get("exec_user") or _get_user_account(runtime) or "anonymous"
        table_suffix = resource.metadata.get("table_suffix", "")
        timeout_sec = min(int(resource.metadata.get("timeout_s", _MAX_MAIN_TIMEOUT_SEC)), _MAX_MAIN_TIMEOUT_SEC)
        poll_interval = max(30.0, float(resource.metadata.get("poll_interval_ms", 30_000)) / 1000.0)

    pt_date = partition_date or default_partition_date()

    # 直连模式：直接查真实目标表 —— 不建表、不插入、不用临时表替换。
    if direct_table:
        target_table = direct_table
        filter_expr = (partition_filter or "").strip() or f"{config.partition_col} = '{pt_date}'"
    else:
        # 临时表名里嵌了同一个日期,所以推导目标表时必须用同一个 pt_date ——
        # 否则会拼出一个不存在的表名,校验静默返回空结果。
        if temp_table is None:
            temp_table = _extract_insert_target_table(sql, user_account, table_suffix, pt_date)
        if not temp_table:
            logger.debug("[quality_check] skipped: cannot extract temp table from sql")
            return None
        target_table = temp_table
        filter_expr = ""

    fields = _extract_fields_from_insert_sql(sql, config.exclude_fields)
    if not fields:
        logger.debug("[quality_check] skipped: no checkable fields in INSERT")
        return None

    checkers = config.checkers
    # 字段类型来自交付的 DDL —— 决定判空写法（STRING 用 bicoredata.IsEmpty，
    # 数值等用 IS NOT NULL）。读不到就退回 IS NOT NULL。
    column_types = parse_ddl_column_types(_find_ddl_in_workspace(runtime))
    statements = build_quality_check_sqls(
        temp_table=target_table,
        fields=fields,
        partition_col=config.partition_col,
        partition_date=pt_date,
        sample_size=config.sample_size,
        checkers=checkers,
        ratio_fields=config.ratio_fields,
        ratio_field_patterns=config.ratio_field_patterns,
        column_types=column_types,
        partition_filter=filter_expr or None,
    )
    if not statements:
        return None

    check_timeout = min(timeout_sec, _MAX_CHECK_TIMEOUT_SEC)
    checker_by_name = {c.name: c for c in checkers}

    async def _run_one(checker_name: str, check_sql: str):
        """跑一条校验 SQL，返回 ``(checker_name, status, job_id, result, error)``。

        异常在这里就地捕获 —— 一条 checker 挂了不能把整个 ``gather`` 炸掉，
        否则连「是哪一条挂的」都拿不到。
        """
        try:
            if coordinator is None:
                status, job_id, result = await _execute_check_sql_prod(check_sql, check_timeout)
            else:
                status, job_id, result = await _execute_check_sql_integration(
                    check_sql, coordinator, check_timeout, poll_interval
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[quality_check] {checker_name} raised: {exc!r}")
            return checker_name, "error", None, None, f"{exc!r}"
        return checker_name, status, job_id, result, ""

    # 三条校验 SQL **并发**提交 —— DataOps 不支持把多个校验项合并成一条语句，
    # 顺序跑的话一条挂了后面全不跑；并发提交后各自独立轮询，互不影响。
    outcomes = await asyncio.gather(
        *(_run_one(name, sql) for name, sql in statements),
        return_exceptions=True,
    )

    parsed_results: list[tuple[str, dict[str, Any]]] = []
    job_ids: list[str] = []
    failed_checks: list[dict[str, Any]] = []
    for outcome in outcomes:
        if isinstance(outcome, BaseException):  # pragma: no cover - _run_one 已兜底
            logger.warning(f"[quality_check] check raised: {outcome!r}")
            continue
        checker_name, status, job_id, result, error_text = outcome
        if job_id:
            job_ids.append(job_id)
        if status != "completed" or not result:
            logger.debug(
                f"[quality_check] {checker_name} did not complete "
                f"status={status!r} job_id={job_id!r}"
            )
            # 一条挂了**不牵连其它条** —— 已经跑出来的结果照样要展示。
            # 这里只把这一条记成 failed，继续收集其它 checker 的结果。
            failed_checks.append(
                {
                    "name": checker_name,
                    "status": status,
                    "reason": error_text or f"status={status!r}",
                }
            )
            continue
        checker = checker_by_name.get(checker_name)
        if checker is None:  # pragma: no cover - 理论上不会发生
            continue
        parsed_results.append(
            (checker_name, parse_checker_result(result, checker, fields, config))
        )

    if not parsed_results:
        # 一条都没跑成 → 确实没有质量结论可言。
        return _empty_result(
            job_ids[-1] if job_ids else None,
            config.sample_size,
            reason="; ".join(
                f"{item['name']} check did not complete: {item['reason']}"
                for item in failed_checks
            )
            or "no check completed",
        )

    merged = merge_checker_results(parsed_results, fields, config, checkers)

    # 失败的 checker 覆盖到的字段，其对应指标是「未知」而不是「不适用」——
    # 单独记一份，供渲染时把状态列标成「未完成」，避免读成「查过了，通过」。
    failed_metrics: dict[str, list[str]] = {}
    for item in failed_checks:
        checker = checker_by_name.get(item["name"])
        if checker is None:
            continue
        for field_name in fields:
            if checker.applies_to_field(field_name, config):
                failed_metrics.setdefault(field_name, []).append(checker.metric_key)

    failed_names = {str(item["name"]) for item in failed_checks}
    completed_checks = [name for name, _ in statements if name not in failed_names]

    return {
        # 有 checker 没跑完就不能说「全部通过」——但**已经跑出来的指标照常给结论**。
        "ok": merged["ok"] and not failed_checks,
        "warnings": merged["warnings"],
        "details": merged["details"],
        "sampled": True,
        "sample_size": config.sample_size,
        "job_id": job_ids[-1] if job_ids else None,
        "job_ids": job_ids,
        # 实际执行了的校验项（没有比率类字段时异常值占比那条 SQL 不会生成）。
        "checks": [name for name, _ in statements],
        # 跑成了的校验项（failed_checks 的补集）。
        "completed_checks": completed_checks,
        # 没跑成的校验项（超时 / 失败 / 抛异常），每项带原因。
        "failed_checks": failed_checks,
        # ``{字段名: [该字段上没算出来的 metric_key]}`` —— 用于渲染时区分
        # 「不适用」（None）和「没算出来」。
        "failed_metrics": failed_metrics,
        # 本轮是部分完成（有的跑成了、有的没跑成）。
        "partial": bool(failed_checks),
        "temp_table": target_table,
        "partition_date": pt_date,
        # 直连模式额外标记：本轮校验的是真实目标表（没走临时表）。
        "direct": bool(direct_table),
    }


# ============================================================================
# Backward-compatible aliases
#
# dataops_validate_tool 历史上直接暴露以下 reasonable-check helper，
# 保留同名转发，避免外部调用方（含既有单测）断裂。
# ============================================================================


def _build_reasonable_check_sql(
    temp_table: str,
    fields: list[str],
    partition_col: str = _DEFAULT_PARTITION_COL,
    partition_date: str = "",
    sample_size: int = _DEFAULT_SAMPLE_SIZE,
) -> str:
    """Deprecated: use :func:`build_quality_check_sqls`（一个 checker 一条 SQL）。"""
    return build_quality_check_sql(temp_table, fields, partition_col, partition_date, sample_size)


def _parse_reasonable_check_result(
    result: dict[str, Any],
    fields: list[str],
    non_null_threshold: float = _DEFAULT_NULL_RATE_THRESHOLD,
    single_value_threshold: float = _DEFAULT_SINGLE_VALUE_THRESHOLD,
) -> dict[str, Any]:
    """Deprecated: use :func:`parse_quality_check_result`."""
    cfg = QualityCheckConfig(
        thresholds={
            NULL_RATE_CHECKER.threshold_key: non_null_threshold,
            SINGLE_VALUE_CHECKER.threshold_key: single_value_threshold,
        }
    )
    return parse_quality_check_result(result, fields, cfg)


def _is_reasonable_check_enabled() -> bool:
    """Deprecated: use :func:`resolve_config`."""
    return _as_bool(os.environ.get("DATAOPS_ENABLE_REASONABLE_CHECK"), True)


_REASONABLE_CHECK_DEFAULT_EXCLUDE_COLS = DEFAULT_EXCLUDE_FIELDS
