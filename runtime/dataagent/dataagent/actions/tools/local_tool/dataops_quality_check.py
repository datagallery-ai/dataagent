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

# DataTaskIR 的**只读**口径读取器 —— 主键重复率 / 抽样回源比对要读已冻结的
# 口径（identity.components / output_features.grain）。
#
# 依赖方向：local_tool -> hooks.examples.data_task_ir_spike，IR 侧不反向依赖
# local_tool，无环。
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.reader import (
    load_committed_fields,
)

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
    cte_names,
    expand_ctes_static,
    extract_insert_target_table,
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
# 十几分钟。与主链路对齐到 1 小时，让慢 SQL 有机会跑完。
_MAX_CHECK_TIMEOUT_SEC = 60 * 60
_MAX_MAIN_TIMEOUT_SEC = 60 * 60

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
# 数据行数阈值（**下限**）：行数少于它就告警。
# 默认 1 —— 「一行都没写进去」是最明确的数据丢失信号。
# 想更严格（要求至少 N 行）就调大 `row_count_threshold`。
_DEFAULT_ROW_COUNT_THRESHOLD = 1
# 比率类字段的默认识别规则：字段名包含 "ratio"。
# 可通过 tool_config / resource metadata 的 ratio_fields、ratio_field_patterns 覆盖。
_DEFAULT_RATIO_FIELD_PATTERNS: tuple[str, ...] = ("ratio",)

# ── 数据正确性校验（新增四条规则）的默认配置 ─────────────────────────────────
#
# 计数型字段的默认识别规则：字段名（小写）包含任一模式即视为计数型候选。
# **候选**还得过类型关：DDL 类型是数值型才真正参与负值校验（见
# :func:`resolve_count_fields`）。类型判不出来 → 跳过并注明原因。
_DEFAULT_COUNT_FIELD_PATTERNS: tuple[str, ...] = ("cnt", "count", "_num", "times", "_times")

# 差值型字段的默认排除模式：``amount_diff`` / ``balance_change`` 这类字段
# 天然可正可负，对它们判负会产生大量误报 —— 误报会让自修正去「修」本来正确的
# SQL，比漏报更糟。
_DEFAULT_NEGATIVE_EXCLUDE_PATTERNS: tuple[str, ...] = ("diff", "delta", "change", "chg")

# 常识检测的抽样行数（要覆盖到分类型的数据，3 行太少）。
_DEFAULT_COMMON_SENSE_SAMPLE_SIZE = 20
# 回源比对的抽样行数（需求原文：抽样三行）。
_DEFAULT_RECONCILE_SAMPLE_SIZE = 3

# 自修正轮次上限。**与 DataOps 提交次数上限（``DEFAULT_DATAOPS_VALIDATION_ATTEMPTS``）
# 是两个独立预算** —— 前者是「修正几轮」，后者是「同一条 SQL 提交几次」，
# 两者互不覆盖。
_DEFAULT_SELF_CORRECT_MAX_ROUNDS = 3

# 回源比对的数值容差：``abs(源表值 - 特征表值) <= max(绝对容差, 相对容差 * |源表值|)``。
_DEFAULT_RELATIVE_TOLERANCE = 1e-6
_DEFAULT_ABSOLUTE_TOLERANCE = 1e-9

#: 主键重复率阈值：必须为 0 —— 任何一行重复即违规。
_DEFAULT_PK_DUPLICATE_THRESHOLD = 0.0

#: 计数型字段负值占比阈值：必须为 0 —— 出现任何负值即违规。
_DEFAULT_NEGATIVE_COUNT_THRESHOLD = 0.0

# 环境变量开关沿用历史命名，向后兼容
_ENABLE_VALUES = frozenset({"true", "1", "yes", "on", "y", "t"})

#: 校验作用域 —— 逐字段指标（默认）vs 表级指标。
#:
#: 逐字段指标（空值率 / 单一值比率 / 异常值占比）都是**比率**，可以基于
#: ``LIMIT N`` 的采样行计算。表级指标（数据行数）**不是比率** —— 它描述的是
#: 整张表有多少行，采样得出来的「前 N 行」没有意义，所以必须走**全量 COUNT**。
_SCOPE_FIELD = "field"
_SCOPE_TABLE = "table"

#: 违规严重级别。
#:
#: - ``error`` —— 确凿的正确性错误，**会触发自修正**；
#: - ``warning`` —— 可疑但不确凿（例：常识判定里模型拿不准的），不触发自修正。
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

# ============================================================================
# Structured violation model
# ============================================================================


@dataclass(frozen=True)
class CorrectnessViolation:
    """一条**结构化**的数据正确性违规。

    为什么要有这个结构：校验结论原本只有 ``warnings: list[str]`` —— 是给人看的
    字符串。自修正需要的是「哪条规则、哪个字段、证据是什么、建议怎么改」，
    光靠字符串喂给模型不可靠。

    ``warnings`` 保留（向后兼容现有前端渲染），但**由 violations 派生** ——
    保证「告警文案」与「结构化违规」永远一致（见 :meth:`to_warning_text`）。

    Attributes:
        rule: 规则名（``pk_duplicate_rate`` / ``negative_count`` /
            ``common_sense`` / ``source_reconciliation``）。
        field: 字段名；表级规则用主键组合描述（如 ``udid`` 或 ``udid+dt``）。
        severity: ``error`` / ``warning``。只有 ``error`` 触发自修正。
        message: 人类可读的结论。
        evidence: 证据（实测值 / 重复样例 / 两侧取值对比）。
        suggestion: 修正方向（喂给模型）。
    """

    rule: str
    field: str
    severity: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""
    #: 已定稿的告警文案（可选）。
    #:
    #: 历史 checker 的 warning 文案已经被前端按字符串消费（``_warned_field_names``
    #: 用 ``'字段名'`` 引号模式反查字段），改文案就会打断现有渲染。所以历史
    #: checker 的违规把原文案原样存进这里，``to_warning_text()`` 直接返回它 ——
    #: 既保证「告警文案由违规派生」，又不改动既有文案。
    warning_text: str = ""

    def to_warning_text(self) -> str:
        """派生出人类可读的告警文案。

        **文案必须能反推回字段名** —— 现有渲染层
        （:func:`_warned_field_names`）用 ``'字段名'`` 的引号模式从告警里
        反查命中告警的字段。表级规则的 ``field`` 是主键组合描述，同样带上引号。
        """
        if self.warning_text:
            return self.warning_text
        prefix = f"[{self.rule}]"
        if self.field:
            return f"{prefix} 字段 '{self.field}' {self.message}"
        return f"{prefix} {self.message}"

    @property
    def is_error(self) -> bool:
        """是否是 error 级（触发自修正的唯一条件）。"""
        return self.severity == SEVERITY_ERROR

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的 dict（报告 / 自修正上下文用）。"""
        return {
            "rule": self.rule,
            "field": self.field,
            "severity": self.severity,
            "message": self.message,
            "evidence": dict(self.evidence or {}),
            "suggestion": self.suggestion,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CorrectnessViolation:
        """从 dict 还原（``to_dict`` 的逆操作）。

        报告与自修正之间要跨进程/跨轮次传违规 —— 用 dict 序列化最省事，
        所以这里提供无损的还原。
        """
        return cls(
            rule=str(raw.get("rule") or ""),
            field=str(raw.get("field") or ""),
            severity=str(raw.get("severity") or SEVERITY_WARNING),
            message=str(raw.get("message") or ""),
            evidence=dict(raw.get("evidence") or {}),
            suggestion=str(raw.get("suggestion") or ""),
        )


def violations_from_dicts(raw: Any) -> list[CorrectnessViolation]:
    """把序列化的违规列表还原成 :class:`CorrectnessViolation` 列表。"""
    if not isinstance(raw, list):
        return []
    result: list[CorrectnessViolation] = []
    for item in raw:
        if isinstance(item, CorrectnessViolation):
            result.append(item)
        elif isinstance(item, dict):
            result.append(CorrectnessViolation.from_dict(item))
    return result


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
    #: 主键列（空元组表示「本轮没有主键信息」）。
    #:
    #: 来源优先级：``DataTaskIR.identity.components`` >
    #: ``DataTaskIR.output_features[].aggregation.grain`` >
    #: ``primary_key_columns`` 配置 > 不可得（此时主键重复率 checker
    #: **不生成 SQL**，记为 skipped）。
    #: 绝不从「排除字段列表」或 INSERT 的 ``GROUP BY`` 反推 —— 前者是按
    #: 「无业务含义」筛的，后者是 SQL 实现细节，都与「业务主键」不是一回事；
    #: 反推出来的主键可能漏掉真实的复合主键成员，导致重复率被低估。
    primary_key_columns: tuple[str, ...] = ()
    #: INSERT 里解析出的源表清单（回源比对用）。
    source_tables: tuple[str, ...] = ()
    #: 原始 INSERT SQL（回源比对用它理解口径，自修正用它做增量修改的底稿）。
    source_sql: str = ""
    #: 字段业务注释（DDL 的 ``COMMENT``）—— 常识判定靠它给出违反依据。
    column_comments: dict[str, str] = field(default_factory=dict)
    #: 显式声明的计数型字段（负值校验的适用范围）。非空时**以清单为准**。
    count_fields: frozenset[str] = frozenset()
    #: 计数型字段名模式（小写子串匹配）。
    count_field_patterns: tuple[str, ...] = _DEFAULT_COUNT_FIELD_PATTERNS
    #: 差值型字段名模式 —— 匹配到的字段不参与负值校验。
    negative_exclude_patterns: tuple[str, ...] = _DEFAULT_NEGATIVE_EXCLUDE_PATTERNS

    def column(self, field_name: str) -> str:
        """反引号包裹列名（已包裹的不重复包裹）。"""
        return field_name if field_name.startswith("`") else f"`{field_name}`"

    def _column_type(self, field_name: str) -> str:
        """查字段类型（**大小写不敏感**）。

        DDL / semantic 层返回的列名大小写不统一（``UDID`` vs ``udid``），
        而 INSERT 投影里的别名是另一种大小写 —— 精确匹配会漏掉类型，
        静默退回 ``IS NOT NULL``，让 STRING 字段的空串被算成「非空」。
        """
        types = self.column_types
        declared = types.get(field_name)
        if declared is None:
            lowered = field_name.lower()
            for key, value in types.items():
                if key.lower() == lowered:
                    declared = value
                    break
        return (declared or "").lower()

    def is_string_field(self, field_name: str) -> bool:
        """字段是否按 **STRING** 存储。

        STRING 存储的字段判空必须用 ``bicoredata.IsEmpty``（空字符串也算空），
        数值/时间戳等非字符串存储则用 ``IS NOT NULL``。
        类型未知（没拿到 DDL）时按非字符串处理 —— ``IS NOT NULL`` 对所有类型都成立，
        比误用 IsEmpty 安全。

        ``varchar(n)`` / ``char(n)`` 也算字符串存储 —— 它们同样是变长/定长字符串，
        空串必须参与「非空率」统计。
        """
        return self._column_type(field_name).startswith(("string", "varchar", "char"))

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

        三态语义（**显式空串 ≠ 未指定**）：

        - ``partition_filter_expr is None`` → 用 ``partition_col`` +
          ``partition_date`` 拼出默认条件（直连真实表、单元测试的默认路径）。
        - ``partition_filter_expr == ""`` → **明确不过滤**（临时表模式）。
        - ``partition_filter_expr`` 非空 → 用它（直连模式显式指定分区）。

        **临时表模式必须显式传 ``""``** —— 临时表是本次试算自己建的，里面
        只有这一次 INSERT 写进去的数据；数据实际落在哪个分区取决于
        **交付物 SELECT 里的 pt_d 表达式**（可能是任意静态日期），不是我们
        拼出来的这个基准日。硬加 ``pt_d = '<today-7>'`` 会让查询恒为 0 行
        → 报「空表」→ 把一次**成功的**试算误判成「一行数据都没校验过」。
        """
        if self.partition_filter_expr is None:
            return f"{self.partition_col} = '{self.partition_date}'"
        return self.partition_filter_expr.strip()

    def where_clause(self, *extra: str) -> str:
        """拼出 ``WHERE ...`` 子句内容；没有任何条件时返回**空串**。

        返回空串时调用方必须**整个省略 WHERE 子句** —— 拼出
        ``WHERE `` 会是语法错误。
        """
        parts = [p for p in (self.partition_filter(), *extra) if p and p.strip()]
        return " AND ".join(parts)


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
    #: (字段名, 上下文) -> 该 checker 是否适用于该字段。
    #:
    #: 与 :attr:`applies_to` 的区别：``applies_to`` 只能看**配置**，而有些判定
    #: 需要**列类型**（如「计数型字段」要 DDL 类型是数值型）—— 列类型只存在于
    #: :class:`CheckContext` 里。两个都为空时该 checker 对所有字段适用。
    applies_to_ctx: Callable[[str, CheckContext], bool] | None = None
    #: 违规严重级别：``error`` 触发自修正，``warning`` 只告警。
    severity: str = SEVERITY_WARNING
    #: 校验作用域：
    #:   ``"field"``（默认）—— 逐字段指标，结果写进 ``details[field][metric_key]``；
    #:   ``"table"`` —— 表级指标（如数据行数），结果写进 ``table_metrics[metric_key]``，
    #:     不参与逐字段的表格渲染。
    scope: str = _SCOPE_FIELD

    def applies_to_field(self, field_name: str, config: QualityCheckConfig) -> bool:
        """该 checker 是否适用于给定字段（只看配置）。"""
        if self.applies_to is None:
            return True
        return bool(self.applies_to(field_name, config))

    def applies_to_field_ctx(
        self,
        field_name: str,
        config: QualityCheckConfig,
        ctx: CheckContext | None = None,
    ) -> bool:
        """该 checker 是否适用于给定字段（配置 + 上下文）。

        需要列类型的判定走这里（``applies_to_ctx``）；上下文缺省时退回只看配置的
        判定 —— 拿不到上下文时宁可「适用但没算出来」，也不谎报「不适用」。
        """
        if self.applies_to_ctx is not None and ctx is not None:
            return bool(self.applies_to_ctx(field_name, ctx))
        return self.applies_to_field(field_name, config)

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
    where = ctx.where_clause()
    where_sql = f"\n  WHERE {where}" if where else ""
    return (
        "(\n"
        f"  SELECT {cols}\n"
        f"  FROM {ctx.temp_table}{where_sql}{limit}\n"
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


# ---------------------------------------------------------------------------
# 内置 checker：数据行数（表级指标）
# ---------------------------------------------------------------------------
#
# 口径：``SELECT COUNT(1) FROM <表> WHERE <分区条件>``
#
# **这是表级指标，不是字段级比率** —— 它回答「这张表（这个分区）里到底有多少
# 行数据」，而不是「某个字段的取值分布好不好」。所以：
#
# 1. **不走采样**。采样能算比率，算不出真实行数 —— 前 10000 行永远是 10000。
#    这条 SQL 是全量 COUNT，是本链路里唯一一条全表扫描的语句。
# 2. **不挂在字段上**。结果写进 ``table_metrics['row_count']``，不参与逐字段
#    的 Markdown 表格（那个表格的列都是比率）。
# 3. **阈值语义是「下限」**。行数太少通常意味着 JOIN 写错了（INNER JOIN 丢数据）、
#    分区过滤条件写错、或者上游数据没落盘 —— 所以违规方向是 ``< 阈值``。
#
# 默认阈值 1（意为「一行都没有」就告警）。想更严格就调大
# ``row_count_threshold``；想彻底关掉这个校验就把 ``checks`` 里去掉
# ``row_count``（或设 ``row_count_enabled: false``）。


def _build_row_count_sql(ctx: CheckContext) -> str:
    """数据行数 SQL —— 表级 COUNT。

    形状::

        SELECT COUNT(1) AS row_count
        FROM <表>
        [WHERE <分区条件>]

    **没有派生表、没有 LIMIT** —— 行数必须数全量。分区条件用与其它校验项
    同一个来源（``ctx.where_clause()``），保证口径一致：同一个分区。

    **临时表模式下没有 WHERE** —— 临时表里只有本次试算写进去的数据，
    数据落在哪个分区由交付物决定，额外过滤会查不到行（详见
    :meth:`CheckContext.partition_filter`）。
    """
    where = ctx.where_clause()
    where_sql = f"\nWHERE {where}" if where else ""
    return (
        "SELECT\n"
        "  COUNT(1) AS row_count\n"
        f"FROM {ctx.temp_table}{where_sql}"
    )


def _row_count_violates(value: float, threshold: float) -> bool:
    # 行数**少于**阈值即违规（行数太少 = 数据丢了 / 过滤条件写错）。
    return value < threshold


def _row_count_warning(field_name: str, value: float, threshold: float) -> str:
    return (
        f"数据行数 {int(value)} < 阈值 {int(threshold)}"
        f"（疑似 JOIN 丢数据 / 分区过滤条件写错 / 上游数据未落盘）"
    )


ROW_COUNT_CHECKER = QualityChecker(
    name="row_count",
    metric_key="row_count",
    threshold_key="row_count_threshold",
    default_threshold=_DEFAULT_ROW_COUNT_THRESHOLD,
    build_sql=_build_row_count_sql,
    violates=_row_count_violates,
    format_warning=_row_count_warning,
    scope=_SCOPE_TABLE,
)


# ---------------------------------------------------------------------------
# 内置 checker：主键重复率（表级指标，全表算）
# ---------------------------------------------------------------------------
#
# 口径（需求 1.1「主键重复率要为 0」）::
#
#     重复率 = (有效行数 - 唯一主键数) / 有效行数
#
# **全表算，不采样**（D3）：重复的两行可能相隔任意距离，采样必然漏检；
# 而「采样能算比率」这个前提对重复率不成立 —— 采样 N 行算出的重复率不是
# 全表重复率。所以这里走的是与 ``row_count`` 同性质的**全量 COUNT**。
#
# **主键为空的行从分母剔除**：主键为空的行本就该被过滤掉（那是空值率校验项
# 的职责），把它算进分母只会稀释重复率，让真正重复的主键被低估。
#
# **实现必须用 ``GROUP BY ... HAVING``，不能用 ``COUNT(DISTINCT ...)``**：
# 生产 DataOps adHoc **硬性拦截** ``COUNT(DISTINCT ...)``，报
# ``AdHoc stage for parsing sql: ERROR: Do not display count(distinct) in
# your hive sql, because there is a performance problem. It is recommended
# that you use group by.``。所以唯一主键数只能通过分组聚合拿到。
#
# **复合主键直接多列 ``GROUP BY``，不做字符串拼接**。
#
# 早先这里用 ``CONCAT_WS('\x01', col_a, col_b)`` 拼成单值再分组 ——
# 那在**生产 adHoc 上被直接拒绝**，实测报错::
#
#     org.apache.spark.sql.AnalysisException: Undefined function: 'CONCAT_WS'.
#     This function is neither a registered temporary function nor a permanent
#     function registered in the database 'agapdw'.
#
# **根因**：生产 adHoc 跑的是 **Spark SQL**（报错栈是
# ``SparkExecuteStatementOperation``），``CONCAT_WS`` 是 **Hive 函数**，
# Spark 没注册它。
#
# **而且这个拼接本来就没必要** —— 内层的 ``pk`` 列**根本没被用到**：
# 外层只聚合 ``cnt``，结果解析只读 ``pk_duplicate_rate`` /
# ``pk_duplicate_rows`` / ``pk_total_rows``。多列 ``GROUP BY`` 语义完全
# 等价（按整组主键分组），却只碰 ``GROUP BY`` / ``COUNT`` / ``SUM`` 这些
# 最基本的构造。
#
# **为什么用派生表而不是 CTE**：生产 adHoc 的表名解析阶段会把
# ``FROM _src`` 当成**物理表名**去解析，直接报「表不存在」。派生表
# ``(SELECT ...) AS _src`` 是标准 SQL，两条链路都支持。

#: 复合主键拼接用的分隔符 —— **已废弃**。
#:
#: 早先复合主键用 ``CONCAT_WS`` 拼成单值再分组，但那在生产 adHoc（Spark SQL）
#: 上被直接拒绝：``Undefined function: 'CONCAT_WS'``。现改为多列
#: ``GROUP BY``，不再需要分隔符。
_PK_JOIN_SEPARATOR = "\x01"


def _build_pk_duplicate_sql(ctx: CheckContext) -> str:
    """主键重复率 SQL —— **全表**，不采样。

    形状::

        SELECT
          CASE WHEN SUM(cnt) = 0 THEN 0
               ELSE SUM(CASE WHEN cnt > 1 THEN cnt - 1 ELSE 0 END) / SUM(cnt)
          END AS pk_duplicate_rate,
          SUM(CASE WHEN cnt > 1 THEN cnt - 1 ELSE 0 END) AS pk_duplicate_rows,
          SUM(cnt) AS pk_total_rows
        FROM (
          SELECT COUNT(1) AS cnt
          FROM <表>
          WHERE <分区条件> AND <主键非空>
          GROUP BY <主键列1>, <主键列2>, ...
        ) AS _pk_groups

    **复合主键用多列 ``GROUP BY``** —— 不拼字符串（见本节上方的说明：
    ``CONCAT_WS`` 是 Hive 函数，生产 adHoc 跑 Spark SQL，不认它）。

    **为什么是「分组后超出的行」而不是「分组数」**：
    ``GROUP BY`` 之后每组一行 ``cnt``。主键唯一 ⟺ 每组 ``cnt = 1``。
    所以「重复的行数」= 所有 ``cnt > 1`` 的组里超出的行数之和
    （``cnt - 1``），「有效行数」= 所有 ``cnt`` 之和。这样**一个 SQL
    同时**给出重复行数与总行数，不需要第二次查询。

    **为什么外层用 ``SUM(cnt)`` 而不是 ``COUNT(1)``**：外层 ``COUNT(1)``
    数的是**分组数**（= 唯一主键数），不是总行数。行数必须由
    ``SUM(cnt)`` 得到。

    **为什么不能写 ``COUNT(DISTINCT ...)``**：生产 adHoc 直接拦截
    （见本节上方的说明），这是实测报错，不是推测。

    **为什么主键非空条件放在 WHERE**：这样 ``cnt`` 的总和就是「主键非空的
    行数」，与「有效行」口径一致（主键为空的行不计入分母）。

    **为什么不用 CTE**：生产 adHoc 的表名解析阶段会把 ``FROM _src`` 当成
    物理表名去解析，报「表不存在」；派生表是唯一安全的写法。
    """
    pk_columns = [ctx.column(name) for name in ctx.primary_key_columns]
    if not pk_columns:
        return ""
    not_null = " AND ".join(f"{ctx.column(name)} IS NOT NULL" for name in ctx.primary_key_columns)
    where_clause = ctx.where_clause(not_null)
    where_sql = f"\n  WHERE {where_clause}" if where_clause else ""
    # 多列 GROUP BY —— 单列与复合主键**同一套写法**，不做字符串拼接。
    group_by = ", ".join(pk_columns)
    return (
        "SELECT\n"
        "  CASE WHEN SUM(cnt) = 0 THEN 0\n"
        "    ELSE SUM(CASE WHEN cnt > 1 THEN cnt - 1 ELSE 0 END) / SUM(cnt)\n"
        "  END AS pk_duplicate_rate,\n"
        "  SUM(CASE WHEN cnt > 1 THEN cnt - 1 ELSE 0 END) AS pk_duplicate_rows,\n"
        "  SUM(cnt) AS pk_total_rows\n"
        "FROM (\n"
        "  SELECT COUNT(1) AS cnt\n"
        f"  FROM {ctx.temp_table}{where_sql}\n"
        f"  GROUP BY {group_by}\n"
        ") AS _pk_groups"
    )


def _pk_duplicate_violates(value: float, threshold: float) -> bool:
    # 重复率必须为 0 —— 超过阈值即违规。
    return value > threshold


def _pk_duplicate_warning(field_name: str, value: float, threshold: float) -> str:
    return (
        f"主键重复率 {value:.6f} > 阈值 {threshold:.6f}"
        f"（主键存在重复，同一主键出现多行）"
    )


PK_DUPLICATE_CHECKER = QualityChecker(
    name="pk_duplicate_rate",
    metric_key="pk_duplicate_rate",
    threshold_key="pk_duplicate_rate_threshold",
    default_threshold=_DEFAULT_PK_DUPLICATE_THRESHOLD,
    build_sql=_build_pk_duplicate_sql,
    violates=_pk_duplicate_violates,
    format_warning=_pk_duplicate_warning,
    severity=SEVERITY_ERROR,
    scope=_SCOPE_TABLE,
)

# ---------------------------------------------------------------------------
# 内置 checker：计数型字段负值占比（字段级指标）
# ---------------------------------------------------------------------------
#
# 口径（需求 1.2「计数型字段应大于等于 0」）::
#
#     负值占比 = SUM(IF(col < 0, 1, 0)) / COUNT(1)
#
# **只对「计数型字段」生效**（D4）：
#   1. 配置 ``count_fields`` 显式清单 → 以清单为准；
#   2. 否则字段名匹配 ``count_field_patterns`` **且** DDL 类型是数值型；
#   3. 类型未知 → 跳过并注明原因。
#
# **差值型字段排除**：``amount_diff`` / ``balance_change`` 这类字段天然可正可负，
# 对它们判负会产生大量误报 —— 误报会让自修正去「修」本来正确的 SQL，比漏报更糟。

#: 数值型 DDL 类型（计数型字段的类型关）。
_NUMERIC_COLUMN_TYPES = (
    "int",
    "bigint",
    "smallint",
    "tinyint",
    "decimal",
    "double",
    "float",
    "numeric",
)


def is_numeric_column_type(column_type: str) -> bool:
    """DDL 类型是否数值型（``INT`` / ``BIGINT`` / ``DECIMAL`` ...）。"""
    text = str(column_type or "").strip().lower()
    if not text:
        return False
    # ``decimal(10,2)`` / ``double`` / ``int`` 之类的判定。
    base = re.split(r"[\s(]", text, maxsplit=1)[0]
    return base in _NUMERIC_COLUMN_TYPES


def resolve_count_fields(
    fields: Sequence[str],
    column_types: dict[str, str],
    *,
    count_fields: frozenset[str] = frozenset(),
    count_field_patterns: Sequence[str] = _DEFAULT_COUNT_FIELD_PATTERNS,
    negative_exclude_patterns: Sequence[str] = _DEFAULT_NEGATIVE_EXCLUDE_PATTERNS,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """判定哪些字段参与「计数型字段非负」校验。

    Returns:
        ``(参与校验的字段, 因类型未知而跳过的字段)``。
    """
    lowered_types = {str(k).lower(): str(v) for k, v in (column_types or {}).items()}
    explicit = {str(c).lower() for c in count_fields}

    def _type_of(name: str) -> str:
        return lowered_types.get(name.lower(), "")

    included: list[str] = []
    skipped_unknown_type: list[str] = []
    for name in fields:
        lowered = name.lower()
        # 差值型字段天然可正可负 —— 一律排除。
        if any(pattern and pattern.lower() in lowered for pattern in negative_exclude_patterns):
            continue
        if count_fields:
            # 显式清单为准：清单里的字段即便类型判不出来也参与校验。
            if lowered in explicit:
                included.append(name)
            continue
        if not any(pattern and pattern.lower() in lowered for pattern in count_field_patterns):
            continue
        column_type = _type_of(name)
        if not column_type:
            # 类型未知 → 不判（跳过并注明原因）。
            skipped_unknown_type.append(name)
            continue
        if not is_numeric_column_type(column_type):
            continue
        included.append(name)
    return tuple(included), tuple(skipped_unknown_type)


def _build_negative_count_sql(ctx: CheckContext) -> str:
    """计数型字段负值占比 SQL —— 一条语句算完所有适用的计数型字段。

    形状::

        SELECT
          SUM(IF(col < 0, 1, 0)) / COUNT(1) AS col,
          SUM(IF(col < 0, 1, 0)) AS neg_rows__col
        FROM (SELECT ... FROM t WHERE ... LIMIT N) AS _src

    ``neg_rows__<col>`` 是**证据列**（负值行数）—— 违规要能说清「负了几行」，
    光有一个占比不够。
    """
    count_fields, _ = resolve_count_fields(
        ctx.fields,
        ctx.column_types,
        count_fields=ctx.count_fields,
        count_field_patterns=ctx.count_field_patterns,
        negative_exclude_patterns=ctx.negative_exclude_patterns,
    )
    if not count_fields:
        return ""
    metrics: list[str] = []
    for name in count_fields:
        col = ctx.column(name)
        metrics.append(f"SUM(IF({col} < 0, 1, 0)) / COUNT(1) AS {name}")
        metrics.append(f"SUM(IF({col} < 0, 1, 0)) AS neg_rows__{name}")
    return _wrap_sample(ctx, metrics, columns=ctx.fields)


def _negative_count_violates(value: float, threshold: float) -> bool:
    # 负值占比必须为 0 —— 超过阈值即违规。
    return value > threshold


def _negative_count_warning(field_name: str, value: float, threshold: float) -> str:
    return (
        f"字段 '{field_name}' 负值占比 {value:.6f} > 阈值 {threshold:.6f}"
        f"（计数型字段不应出现负值）"
    )


def _negative_count_applies(field_name: str, ctx: CheckContext) -> bool:
    """该字段是否是「计数型字段」（参与负值校验）。

    判定口径与 :func:`resolve_count_fields` 完全一致 —— SQL 生成与结果解析
    必须用同一套规则，否则会出现「SQL 里算了这个字段但解析时又跳过」、
    或者「SQL 里没算但解析时告警」的错位。
    """
    included, _ = resolve_count_fields(
        [field_name],
        ctx.column_types,
        count_fields=ctx.count_fields,
        count_field_patterns=ctx.count_field_patterns,
        negative_exclude_patterns=ctx.negative_exclude_patterns,
    )
    return bool(included)


NEGATIVE_COUNT_CHECKER = QualityChecker(
    name="negative_count",
    metric_key="negative_count",
    threshold_key="negative_count_threshold",
    default_threshold=_DEFAULT_NEGATIVE_COUNT_THRESHOLD,
    build_sql=_build_negative_count_sql,
    violates=_negative_count_violates,
    format_warning=_negative_count_warning,
    applies_to_ctx=_negative_count_applies,
    severity=SEVERITY_ERROR,
)

# 全部内置 checker —— 新增校验只需在这里追加一项
#
# **这里只放「历史质量指标」**（比率类 + 数据行数）。新增的**正确性**校验
# （主键重复率 / 计数型字段负值）在 :data:`_CORRECTNESS_CHECKERS` 里 ——
# 两者的默认启用策略、适用性判定与结果形状都不同，混在一个注册表里会让
# 「details 的形状是稳定的」这条不变量变得不可控。
_BUILTIN_CHECKERS: tuple[QualityChecker, ...] = (
    NULL_RATE_CHECKER,
    SINGLE_VALUE_CHECKER,
    OUTLIER_RATIO_CHECKER,
    ROW_COUNT_CHECKER,
)

#: 新增的**数据正确性**校验（需求 1.1 / 1.2）。
#:
#: 与历史 checker 的区别：
#: - 默认**全部启用**（``quality_check.correctness_checks`` 可关）；
#: - 违规 severity 是 ``error`` —— **会触发自修正**；
#: - 主键重复率**全表算**（不采样），计数型字段负值按样本算；
#: - **适用性是动态的**：没有主键信息就没有主键重复率，没有计数型字段就没有
#:   负值校验 —— 这两项不适用时**不进入** ``details`` / ``table_metrics``，
#:   避免在报告里出现「未完成」这种读起来像「查了没查到」的噪音。
_CORRECTNESS_CHECKERS: tuple[QualityChecker, ...] = (
    PK_DUPLICATE_CHECKER,
    NEGATIVE_COUNT_CHECKER,
)

#: 全部可用 checker（历史 + 正确性）—— 供「按名称选择」使用。
ALL_CHECKERS: tuple[QualityChecker, ...] = _BUILTIN_CHECKERS + _CORRECTNESS_CHECKERS

_BUILTIN_BY_NAME: dict[str, QualityChecker] = {c.name: c for c in _BUILTIN_CHECKERS}
_CORRECTNESS_BY_NAME: dict[str, QualityChecker] = {c.name: c for c in _CORRECTNESS_CHECKERS}


def select_checkers(names: Sequence[str] | None) -> tuple[QualityChecker, ...]:
    """按名称挑出生效的**历史质量** checker；``None`` / 空表示全部内置。"""
    if not names:
        return _BUILTIN_CHECKERS
    selected = tuple(_BUILTIN_BY_NAME[n] for n in names if n in _BUILTIN_BY_NAME)
    return selected or _BUILTIN_CHECKERS


def select_correctness_checkers(names: Sequence[str] | None) -> tuple[QualityChecker, ...]:
    """按名称挑出生效的**正确性** checker。

    ``None`` 表示「未配置」→ **全部启用**（需求里四条新规则默认全部开启）。
    非空列表表示「显式清单」→ 只启用清单里的（可以借此单独关掉某条）。
    空列表 → 全部关掉。
    """
    if names is None:
        return _CORRECTNESS_CHECKERS
    return tuple(_CORRECTNESS_BY_NAME[n] for n in names if n in _CORRECTNESS_BY_NAME)


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


def parse_ddl_partition_columns(ddl_sql: str) -> tuple[str, ...]:
    """从 Hive DDL 里解析分区列（``PARTITIONED BY (ds, hr)``）。

    **分区列名不能硬编码成 ``pt_d``** —— 交付物按 ``ds`` / ``dt`` / ``p_date``
    分区时是常态。分区列名错了，分区过滤就查不到行 → 报「空表」。

    返回值是**有序**的元组（第一个是主分区列）。
    """
    if not ddl_sql:
        return ()
    text = re.sub(r"/\*.*?\*/", " ", ddl_sql, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)

    match = re.search(r"PARTITIONED\s+BY\s*\(", text, re.IGNORECASE)
    if not match:
        return ()
    depth = 0
    close_idx = -1
    for i in range(match.end() - 1, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                close_idx = i
                break
    if close_idx < 0:
        return ()

    body = text[match.end() : close_idx]
    columns: list[str] = []
    for part in _split_top_level_commas(body):
        # ``ds STRING`` / ``ds STRING COMMENT '...'``
        name_match = re.match(r"\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\b", part)
        if not name_match:
            continue
        name = name_match.group(1)
        if name not in columns:
            columns.append(name)
    return tuple(columns)


def parse_ddl_columns_with_comments(
    ddl_sql: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """从 Hive DDL 里同时解析 ``{列名: 类型}`` 与 ``{列名: COMMENT}``。

    常识判定（需求 1.3）靠字段的**业务注释**给出违反依据 —— 没有注释时只能
    靠字段名 + 类型 + 样本值，判定质量下降但不阻断（降级路径）。

    Args:
        ddl_sql: 交付的 ``CREATE EXTERNAL TABLE ...`` 语句。

    Returns:
        ``(column_types, column_comments)`` —— 两个都是 ``{列名: 值}``。
        解析不出列清单时返回 ``({}, {})``。
    """
    if not ddl_sql:
        return {}, {}
    # 去掉注释，避免注释里的括号干扰列清单切分。
    text = re.sub(r"/\*.*?\*/", " ", ddl_sql, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)

    open_m = re.search(r"\((?!\s*PARTITIONED\s+BY\b)", text, re.IGNORECASE)
    if not open_m:
        return {}, {}
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
        return {}, {}

    body = text[open_m.end() : close_idx]
    column_types: dict[str, str] = {}
    column_comments: dict[str, str] = {}
    for part in _split_top_level_commas(body):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"`?([A-Za-z_][A-Za-z0-9_]*)`?\s+([A-Za-z_][A-Za-z0-9_]*)", part)
        if not m:
            continue
        name = m.group(1)
        column_types[name] = m.group(2).lower()
        comment = _extract_ddl_comment(part)
        if comment:
            column_comments[name] = comment
    return column_types, column_comments


def _extract_ddl_comment(part: str) -> str:
    """从一个列定义片段里抽出 ``COMMENT '...'`` 的文本。"""
    match = re.search(
        r"\bCOMMENT\s+'((?:[^']|'')*)'",
        part,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return match.group(1).replace("''", "'").strip()


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


def derive_insert_partition_literals(sql: str, partition_date: str = "") -> dict[str, str]:
    """从 INSERT 的 ``PARTITION (...)`` 子句里解析出**静态的分区字面量**。

    ``INSERT OVERWRITE TABLE t PARTITION (pt_d = '20261020')`` → ``{"pt_d": "20261020"}``。

    **为什么需要它**：临时表是**分区表**（DDL 的 ``PARTITIONED BY`` 被原样保留），
    而 Spark / Hive 的分析器会拒绝扫描分区表却没有分区条件的语句
    （``partition condition not found in where or on node``）。所以临时表模式
    **不能简单地「不过滤」** —— 但也不能用拼出来的基准日去过滤（那会查不到行）。

    正确做法是：**用这次 INSERT 实际写进去的那个分区值**去过滤 ——
    口径和数据完全一致，既满足分区裁剪，又能查到行。

    **只认静态字面量**（``'20261020'``）。分区值是表达式 / 占位符
    （``$date`` / ``${date}`` / 函数调用）时解析不出来 —— 此时调用方应退回
    「不过滤」的降级路径。

    Args:
        sql: INSERT SQL。
        partition_date: 试算链路的基准日（``YYYYMMDD``）。用于把
            ``$date`` / ``${date}`` 占位符替换成实际写入的日期。

    Returns:
        ``{分区列名: 分区值}``；解析不出时返回空 dict。
    """
    if not sql or "PARTITION" not in sql.upper():
        return {}

    text = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)

    match = re.search(r"\bPARTITION\s*\(", text, re.IGNORECASE)
    if not match:
        return {}

    depth = 0
    close_idx = -1
    for i in range(match.end() - 1, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                close_idx = i
                break
    if close_idx < 0:
        return {}

    body = text[match.end() : close_idx]
    literals: dict[str, str] = {}
    for part in _split_top_level_commas(body):
        kv = re.match(
            r"\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s*=\s*'([^']*)'",
            part,
            re.IGNORECASE,
        )
        if not kv:
            # 分区值不是字符串字面量（表达式 / 数字 / 占位符）→ 放弃解析。
            continue
        col, value = kv.group(1), kv.group(2).strip()
        if not col or not value:
            continue
        # 占位符 → 换成试算实际写入的基准日。
        if value.lower() in ("$date", "${date}") and partition_date:
            value = partition_date
        elif "$" in value or "{" in value:
            # 其它调度占位符（``${start_time,...}``）→ 无法静态求值。
            continue
        literals[col] = value
    return literals


def build_temp_table_partition_filter(
    partition_literals: dict[str, str],
    partition_col: str,
) -> str:
    """把解析出的分区字面量拼成 WHERE 里的过滤条件。

    Args:
        partition_literals: :func:`derive_insert_partition_literals` 的结果。
        partition_col: 主分区列名（当字面量里没有它时作为兜底尝试对象）。

    Returns:
        形如 ``pt_d = '20261020'`` 的条件串；**多个分区列**时用 `` AND `` 连接。
        解析不出任何字面量时返回**空串**（调用方据此退回「不过滤」）。
    """
    if not partition_literals:
        return ""

    parts = [
        f"{col} = '{value}'"
        for col, value in partition_literals.items()
        if col and value
    ]
    if not parts and partition_col:
        # 字面量里没有主分区列 → 没法构造条件。
        return ""
    return " AND ".join(parts)


def _find_ddl_in_workspace(runtime: Any, target_table: str = "") -> str:
    """在 workspace 里找交付的 ``create_*.sql``。

    **按目标表名匹配** —— 多表交付时 workspace 里可能有多个 ``create_*.sql``，
    拿错那一份会把 A 表的列类型套到 B 表上（STRING 被当成数值 → 判空口径
    直接错），分区列也会认错。

    Args:
        runtime: 运行时上下文（提供 ``workspace_dir``）。
        target_table: 目标表（``db.table`` 或裸表名）。留空时退回「最新修改的
            那个」。

    Returns:
        DDL 文本；找不到返回空串。
    """
    raw = getattr(runtime, "workspace_dir", None) if runtime is not None else None
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        return ""
    try:
        workspace = Path(raw).expanduser().resolve()
    except OSError:
        return ""
    if not workspace.is_dir():
        return ""

    candidates: list[Path] = []
    for directory in (workspace / "sql", workspace):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            name = path.name.lower()
            if name.startswith("create_") and name.endswith(".sql") and path.is_file():
                candidates.append(path)
    if not candidates:
        return ""

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    if target_table.strip():
        from dataagent.actions.tools.local_tool.dataops_table_schema import (
            extract_ddl_table_ref,
        )

        wanted = _normalize_table_ref(target_table)
        matched: list[Path] = []
        for path in candidates:
            try:
                ddl_sql = path.read_text(encoding="utf-8")
            except OSError:
                continue
            # 用**认裸表名**的解析器 —— 交付物的 DDL 常常不带库名
            # （``CREATE TABLE ads_x``），只认 ``db.table`` 的话这种 DDL
            # 永远匹配不上 → 退回「按 mtime 取最新」→ 可能拿到**另一张表**的 DDL。
            declared = extract_ddl_table_ref(ddl_sql)
            if declared and _table_refs_match(declared, wanted):
                matched.append(path)
        if matched:
            try:
                return max(matched, key=_mtime).read_text(encoding="utf-8")
            except OSError:
                return ""

    try:
        return max(candidates, key=_mtime).read_text(encoding="utf-8")
    except OSError:
        return ""


def _normalize_table_ref(ref: str) -> str:
    """把表引用归一化成小写的 ``db.table``（去掉反引号）。"""
    parts = [p.strip().strip("`").strip('"') for p in (ref or "").split(".")]
    return ".".join(p for p in parts if p).lower()


def _table_refs_match(left: str, right: str) -> bool:
    """两个表引用是否指同一张表。

    **裸表名视为匹配任何同名表** —— DDL 可能不带库名（``CREATE TABLE ads_x``），
    而 DML 里是 ``biads.ads_x``；这时按最后一段（表名）比对。
    """
    left_norm = _normalize_table_ref(left)
    right_norm = _normalize_table_ref(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    return left_norm.split(".")[-1] == right_norm.split(".")[-1]


def _column_types_for_table(runtime: Any, target_table: str) -> dict[str, str]:
    """从 workspace 的 DDL 里取**指定那张表**的列类型。

    直连真实目标表时，workspace 里的 ``create_*.sql`` 可能是**另一张表**的
    （比如 agent 这次只交付了 DML，或者同时交付了多张表的 DDL）。拿别表的类型
    套到目标表的同名列上，会把 STRING 列当成数值列 —— 判空口径直接错
    （STRING 要用 ``bicoredata.IsEmpty``，空串才算空）。

    所以这里只在 **DDL 的表名与目标表一致**时才采用它的类型；否则返回空 dict，
    调用方退回 ``IS NOT NULL``（对所有类型都成立，比用错类型安全）。
    """
    ddl_sql = _find_ddl_in_workspace(runtime, target_table)
    if not ddl_sql:
        return {}
    # 延迟导入避免循环依赖（dataops_table_schema → dataops_quality_check）。
    from dataagent.actions.tools.local_tool.dataops_table_schema import (
        extract_ddl_table_ref,
    )

    ddl_table = extract_ddl_table_ref(ddl_sql)
    if not ddl_table or not _table_refs_match(ddl_table, str(target_table or "")):
        return {}
    return parse_ddl_column_types(ddl_sql)


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
    #: 生效的**正确性** checker 名称（``None`` → 全部启用）。
    correctness_checker_names: tuple[str, ...] | None = tuple(
        c.name for c in _CORRECTNESS_CHECKERS
    )
    thresholds: dict[str, float] = field(default_factory=dict)
    #: 显式声明的比率类字段（异常值占比校验只对这些字段生效）。
    ratio_fields: frozenset[str] = frozenset()
    #: 比率类字段名模式（小写子串匹配）；为空表示不做模式匹配。
    ratio_field_patterns: tuple[str, ...] = _DEFAULT_RATIO_FIELD_PATTERNS
    #: 显式声明的主键列（主键重复率校验用）。
    primary_key_columns: tuple[str, ...] = ()
    #: 显式声明的计数型字段（负值校验用）—— **以清单为准**，清单外字段一律不算计数型。
    count_fields: frozenset[str] = frozenset()
    #: 计数型字段名模式（小写子串匹配）。
    count_field_patterns: tuple[str, ...] = _DEFAULT_COUNT_FIELD_PATTERNS
    #: 差值型字段名模式 —— 匹配到的字段**不参与**负值校验。
    negative_exclude_patterns: tuple[str, ...] = _DEFAULT_NEGATIVE_EXCLUDE_PATTERNS
    #: 常识检测的抽样行数。
    common_sense_sample_size: int = _DEFAULT_COMMON_SENSE_SAMPLE_SIZE
    #: 回源比对的抽样行数。
    reconcile_sample_size: int = _DEFAULT_RECONCILE_SAMPLE_SIZE
    #: 自修正开关（关闭后只保留告警，不调模型改 SQL）。
    self_correct_enabled: bool = True
    #: 自修正轮次上限（独立于 DataOps 提交次数上限）。
    self_correct_max_rounds: int = _DEFAULT_SELF_CORRECT_MAX_ROUNDS
    #: 回源比对的数值容差。
    relative_tolerance: float = _DEFAULT_RELATIVE_TOLERANCE
    absolute_tolerance: float = _DEFAULT_ABSOLUTE_TOLERANCE

    @property
    def checkers(self) -> tuple[QualityChecker, ...]:
        """本次实际生效的**历史质量** checker 实例列表。"""
        return select_checkers(self.checker_names)

    @property
    def correctness_checkers(self) -> tuple[QualityChecker, ...]:
        """本次实际生效的**正确性** checker 实例列表。"""
        return select_correctness_checkers(self.correctness_checker_names)

    def threshold_for(self, checker: QualityChecker) -> float:
        return self.thresholds.get(checker.threshold_key, checker.default_threshold)

    def is_ratio_field(self, field_name: str) -> bool:
        """判断字段是否属于「比率类」（与 :meth:`CheckContext.is_ratio_field` 同规则）。"""
        if self.ratio_fields:
            return field_name in self.ratio_fields
        lowered = field_name.lower()
        return any(pattern and pattern.lower() in lowered for pattern in self.ratio_field_patterns)

    def is_negative_excluded_field(self, field_name: str) -> bool:
        """字段是否属于「差值型」（差值型字段天然可正可负，不参与负值校验）。"""
        lowered = field_name.lower()
        return any(pattern and pattern.lower() in lowered for pattern in self.negative_exclude_patterns)


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
        for key in (
            "null_rate_threshold",
            "single_value_threshold",
            "outlier_ratio_threshold",
            "row_count_threshold",
            "pk_duplicate_rate_threshold",
            "negative_count_threshold",
        ):
            if key in source:
                raw_thresholds[key] = source[key]

    thresholds = {c.threshold_key: c.default_threshold for c in _BUILTIN_CHECKERS}
    for checker in _BUILTIN_CHECKERS:
        for key in (checker.metric_key, checker.threshold_key):
            if key in raw_thresholds:
                thresholds[checker.threshold_key] = _as_float(
                    raw_thresholds[key], checker.default_threshold
                )
    # 正确性 checker 的阈值独立解析 —— 它们不在 ``_BUILTIN_CHECKERS`` 里。
    for checker in _CORRECTNESS_CHECKERS:
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

    # --- 生效的正确性 checker ---
    # 独立于 ``checks``：``checks`` 是历史质量指标的清单，而正确性校验默认
    # **全部启用**（需求：四条新规则默认全开），要关掉得显式写
    # ``correctness_checks: []`` 或只列其中一条。
    correctness_raw = _pick(sections, "correctness_checks")
    if isinstance(correctness_raw, (list, tuple)):
        correctness_checker_names: tuple[str, ...] | None = tuple(
            str(n) for n in correctness_raw
        )
    else:
        correctness_checker_names = tuple(c.name for c in _CORRECTNESS_CHECKERS)

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

    # --- 主键列（主键重复率校验的适用范围） ---
    pk_raw = _pick(sections, "primary_key_columns")
    primary_key_columns = (
        tuple(str(c) for c in pk_raw if str(c).strip())
        if isinstance(pk_raw, (list, tuple, set))
        else ()
    )

    # --- 计数型字段判定（负值校验的适用范围） ---
    count_raw = _pick(sections, "count_fields")
    count_fields = (
        frozenset(str(c) for c in count_raw)
        if isinstance(count_raw, (list, tuple, set))
        else frozenset()
    )
    count_patterns_raw = _pick(sections, "count_field_patterns")
    count_field_patterns = (
        tuple(str(p) for p in count_patterns_raw)
        if isinstance(count_patterns_raw, (list, tuple))
        else _DEFAULT_COUNT_FIELD_PATTERNS
    )
    exclude_patterns_raw = _pick(sections, "negative_exclude_patterns")
    negative_exclude_patterns = (
        tuple(str(p) for p in exclude_patterns_raw)
        if isinstance(exclude_patterns_raw, (list, tuple))
        else _DEFAULT_NEGATIVE_EXCLUDE_PATTERNS
    )

    # --- 抽样量 / 自修正 ---
    common_sense_sample_size = _as_int(
        _pick(sections, "common_sense_sample_size"), _DEFAULT_COMMON_SENSE_SAMPLE_SIZE
    )
    reconcile_sample_size = _as_int(
        _pick(sections, "reconcile_sample_size"), _DEFAULT_RECONCILE_SAMPLE_SIZE
    )
    self_correct_enabled = _as_bool(_pick(sections, "self_correct_enabled"), True)
    self_correct_max_rounds = max(
        0, _as_int(_pick(sections, "self_correct_max_rounds"), _DEFAULT_SELF_CORRECT_MAX_ROUNDS)
    )

    # --- 回源比对容差 ---
    relative_tolerance = _as_float(
        _pick(sections, "relative_tolerance"), _DEFAULT_RELATIVE_TOLERANCE
    )
    absolute_tolerance = _as_float(
        _pick(sections, "absolute_tolerance"), _DEFAULT_ABSOLUTE_TOLERANCE
    )

    return QualityCheckConfig(
        enabled=enabled,
        sample_size=sample_size,
        partition_col=partition_col,
        exclude_fields=exclude_fields,
        checker_names=checker_names,
        correctness_checker_names=correctness_checker_names,
        thresholds=thresholds,
        ratio_fields=ratio_fields,
        ratio_field_patterns=ratio_field_patterns,
        primary_key_columns=primary_key_columns,
        count_fields=count_fields,
        count_field_patterns=count_field_patterns,
        negative_exclude_patterns=negative_exclude_patterns,
        common_sense_sample_size=common_sense_sample_size,
        reconcile_sample_size=reconcile_sample_size,
        self_correct_enabled=self_correct_enabled,
        self_correct_max_rounds=self_correct_max_rounds,
        relative_tolerance=relative_tolerance,
        absolute_tolerance=absolute_tolerance,
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
    primary_key_columns: tuple[str, ...] = (),
    source_tables: tuple[str, ...] = (),
    source_sql: str = "",
    column_comments: dict[str, str] | None = None,
    count_fields: frozenset[str] | None = None,
    count_field_patterns: tuple[str, ...] | None = None,
    negative_exclude_patterns: tuple[str, ...] | None = None,
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
        primary_key_columns=tuple(primary_key_columns or ()),
        source_tables=tuple(source_tables or ()),
        source_sql=source_sql or "",
        column_comments=dict(column_comments or {}),
        count_fields=count_fields if count_fields is not None else frozenset(),
        count_field_patterns=(
            count_field_patterns
            if count_field_patterns is not None
            else _DEFAULT_COUNT_FIELD_PATTERNS
        ),
        negative_exclude_patterns=(
            negative_exclude_patterns
            if negative_exclude_patterns is not None
            else _DEFAULT_NEGATIVE_EXCLUDE_PATTERNS
        ),
    )


def build_statements_from_context(ctx: CheckContext) -> list[tuple[str, str]]:
    """按上下文生成 ``[(checker_name, sql), ...]``。

    与 :func:`build_quality_check_sqls` 的区别：后者自己组装上下文（历史 API），
    本函数直接吃一个已经组装好的上下文 —— 编排层需要**同一个**上下文同时用于
    生成 SQL 与解析结果（否则主键 / 列类型 / 计数型字段判定会各算各的）。
    """
    statements: list[tuple[str, str]] = []
    for checker in ctx.checkers:
        sql = checker.build_sql(ctx)
        if sql:
            statements.append((checker.name, sql))
    return statements


# ============================================================================
# Primary key / source table resolution
# ============================================================================


def resolve_primary_key_columns(
    runtime: Any,
    fields: Sequence[str],
    config: QualityCheckConfig | None = None,
    *,
    semantic_primary_keys: Sequence[str] = (),
) -> tuple[str, ...]:
    """解析主键列。

    主键**只**来自 DataTaskIR 的口径声明或显式配置 —— 优先级：

    1. ``DataTaskIR.identity.components`` —— 明细/特征任务的逻辑主键；
    2. ``DataTaskIR.output_features[].aggregation.grain`` —— 聚合表
       （DM 汇总表）的记录粒度；
    3. ``quality_check.primary_key_columns`` 配置 —— 非 IR 场景显式指定；
    4. ``semantic_primary_keys`` —— semantic-service 元数据里的主键标记；
    5. 都拿不到 → 返回空元组（调用方**跳过**主键重复率校验，不猜）。

    **绝不从 INSERT 的 ``GROUP BY`` 推导**：``GROUP BY`` 是 SQL 的**实现细节**，
    不是业务主键的权威声明 —— 它可能多带分区列、可能按月累加导致本就不唯一、
    也可能与业务主键粒度不同。业务主键只认 TASKIR 的口径声明。

    **绝不从「排除字段列表」反推** —— 那份列表是按「无业务含义」筛的，
    与「主键」不是一回事；反推出来的主键可能漏掉真实的复合主键成员，
    导致重复率被低估。

    **单一主键还是复合主键，由声明本身决定** —— ``identity.components`` 与
    ``grain`` 都是**列表**，天然支持复合；校验时整组一起用（多列
    ``GROUP BY``），不拆开。

    **候选集按特异性降序尝试**（每组都必须「全有或全无」匹配上目标表的列）：

    1. ``identity.components ∪ grain`` —— 两处声明的并集。IR 模板写明
       「若某列既是最终记录主键又是聚合分组键，只在 identity 声明；
       ``aggregation.grain`` 可以**引用**同一 field_ref」—— 两处是同一套
       口径的两个视角，并起来才是完整的主键。这条同时修掉「identity 只填了
       1 列、真实粒度是 2 列」的漏填（生产实测 0.866412 误报的根因）。
    2. ``grain`` 单独 —— 聚合表只有粒度、没有逻辑主键时。
    3. ``identity.components`` 单独 —— 明细表只有逻辑主键时。

    逐个尝试而不是取第一个就返回：TASKIR 的两处声明可能只填了其中一处、
    或其中一处在目标表里对不上（分区列被排除等）。取并集最完整，但如果并集
    对不上（多了一列表里没有的），就退到单集 —— 只要**有**一组能完整匹配上
    就用它，匹配不上的组直接作废（见 :func:`_match_primary_key_columns`），
    绝不降级成子集。

    **为什么 semantic 元数据排在配置之后**：配置是人为这张表**显式**写的
    口径（最具体的意图）；semantic 元数据是平台侧对**物理表**的标记
    （通用，但可能只标了复合主键的一部分）。TASKIR 是业务口径声明，
    优先级最高。

    **为什么 semantic 也走「全有或全无」**：它给的是一组列名，可能只标了
    复合主键的一部分 —— 静默降级成子集就是「重复率极高」的误报。
    """
    cfg = config or resolve_config(None)
    identity_columns = _primary_key_from_ir(runtime)
    grain_columns = _primary_key_from_ir_grain(runtime)

    # 候选集按特异性降序 —— 先并集（最完整），再各单集。
    candidates: list[tuple[str, ...]] = []
    union = tuple(dict.fromkeys([*identity_columns, *grain_columns]))
    if union:
        candidates.append(union)
    for single in (grain_columns, identity_columns):
        if single and single not in candidates:
            candidates.append(single)

    for candidate in candidates:
        matched = _match_primary_key_columns(candidate, fields)
        if matched:
            return matched
        logger.debug(
            "[quality_check] TASKIR primary key candidate %s not fully present in "
            "table fields %s -> rejected (must not degrade to a subset)",
            list(candidate),
            list(fields),
        )

    if cfg.primary_key_columns:
        matched = _match_primary_key_columns(cfg.primary_key_columns, fields)
        if matched:
            return matched

    # semantic 元数据兜底 —— 独立数据校验链路（没建 IR）靠它才拿得到主键。
    # 同样走「全有或全无」：它给的是一组列名，可能只标了复合主键的一部分，
    # 静默降级成子集就是「重复率极高」的误报。
    if semantic_primary_keys:
        matched = _match_primary_key_columns(semantic_primary_keys, fields)
        if matched:
            return matched
        logger.debug(
            "[quality_check] semantic primary key candidate %s not fully present in "
            "table fields %s -> rejected (must not degrade to a subset)",
            list(semantic_primary_keys),
            list(fields),
        )
    return ()


def _ir_field_values(runtime: Any) -> list[dict[str, Any]]:
    """取 DataTaskIR 的字段值列表（``[{field_id, value}, ...]``）。

    不能只读 runtime 缓存 —— ``ir_field_values`` 是进程内缓存，每轮 ``chat()``
    新建，只有触发 IR post-hook 的那一轮有值；「第二轮对话执行数据校验」
    不触发 hook → 缓存为空 → 主键拿不到 → 主键重复率与回源比对整项跳过。

    权威口径本来就在持久化的 IR 会话快照里（*"Runtime caches are never the
    source of truth."*），所以走 :func:`load_committed_fields` 的三级回落：
    缓存 → 本轮快照（``prepare_turn`` 恢复）→ 磁盘上最近一次 committed 快照。

    不挂 ``ensure_ir_ready`` hook —— 那个 hook 会触发 IR 刷新的 LLM 调用，
    且 HITL 队列未清时直接 raise，会把「执行数据校验」打断成失败的任务。
    校验链路要的是**读**已冻结的口径，不是**刷新**口径。
    """
    if runtime is None:
        return []
    return load_committed_fields(runtime)


def _primary_key_from_ir_grain(runtime: Any) -> tuple[str, ...]:
    """从 DataTaskIR 的 ``output_features[].aggregation.grain`` 取聚合粒度。

    **为什么这也算 TASKIR 的主键声明**：聚合表（DM 汇总表）的记录主键
    就是它的聚合粒度 —— ``GROUP BY a, b`` 产出 ``(a, b)`` 唯一，此时
    ``(a, b)`` 就是这张表的主键。IR 的 ``identity`` 字段负责「明细/特征
    任务的逻辑主键」，而 DM 汇总表的粒度按 IR 模板的约定记在
    ``output_features`` 各列的 ``aggregation.grain`` 里（模板明确写：
    「若某列既是最终记录主键又是聚合分组键，只在 identity 声明；
    ``aggregation.grain`` 可以**引用**同一 field_ref」）。

    所以两处都是 TASKIR 的权威口径 —— 取的是**已冻结的口径**，不是从 SQL
    反推的实现细节。

    多个输出列的 grain 可能不同（多粒度场景）—— 只取**所有非空 grain 的
    并集**：这些列合起来才构成表的记录粒度。
    """
    if runtime is None:
        return ()
    raw = _ir_field_values(runtime)
    if not raw:
        return ()
    columns: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("field_id") or "") != "output_features":
            continue
        value = item.get("value")
        if not isinstance(value, dict):
            continue
        for entry in _iter_output_feature_entries(value):
            aggregation = entry.get("aggregation")
            if not isinstance(aggregation, dict):
                continue
            grain = aggregation.get("grain")
            if not isinstance(grain, (list, tuple)):
                continue
            for ref in grain:
                text = str(ref or "").strip()
                if text:
                    # ``field_ref`` 可能是 ``db.table.column`` —— 取最后一段。
                    columns.append(text.split(".")[-1].strip("`"))
    return tuple(dict.fromkeys(columns))


def _iter_output_feature_entries(value: dict[str, Any]) -> list[dict[str, Any]]:
    """遍历 ``output_features`` 下的所有列定义。

    ``output_features`` 有三种列容器：``measures``（指标）、``extras``
    （单列扩展）、``features``（扁平列）。三者都可能带 ``aggregation.grain``。
    """
    entries: list[dict[str, Any]] = []
    for container in ("measures", "extras", "features"):
        items = value.get(container)
        if not isinstance(items, (list, tuple)):
            continue
        for item in items:
            if isinstance(item, dict):
                entries.append(item)
    return entries


def _match_primary_key_columns(candidates: Sequence[str], fields: Sequence[str]) -> tuple[str, ...]:
    """把主键声明整组对上目标表里真实存在的列（**大小写不敏感**）。

    **必须「全有或全无」** —— 这是复合主键场景最容易踩的坑：

    主键声明是 ``(a, b)`` 复合主键，但目标表里只有 ``a``（``b`` 是分区列、
    或被排除字段过滤掉了）。如果**丢掉对不上的成员**、只拿 ``a`` 去算，
    就把复合主键**静默降级成单列主键** —— 而单列 ``a`` 在复合粒度的表里
    天然有大量重复，会报出「主键重复率极高」的**误报**。

    生产实测就是这个坑：IR 声明了 ``(app_second_class_cn_name,
    conversionbehaviorid)`` 两列主键，匹配上 1 列、丢掉 1 列，
    262 行里 227 行被判成「重复」，重复率 0.866412 —— 而真实重复率是 0。

    所以：**对不上就整组作废**（返回空元组），让调用方**跳过**该项校验并
    在报告里注明原因。宁可漏查，也不能用错的主键给出假结论。
    """
    by_lower = {str(name).lower(): str(name) for name in fields}
    matched: list[str] = []
    for candidate in candidates:
        text = str(candidate).strip().strip("`")
        if not text:
            continue
        actual = by_lower.get(text.lower())
        if actual is None:
            # 少一个成员就不成立 —— 不能降级。
            return ()
        if actual not in matched:
            matched.append(actual)
    return tuple(matched)


def _primary_key_from_ir(runtime: Any) -> tuple[str, ...]:
    """从 DataTaskIR 的 ``identity.components`` 里取主键列。"""
    if runtime is None:
        return ()
    raw = _ir_field_values(runtime)
    if not raw:
        return ()
    columns: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("field_id") or "") != "identity":
            continue
        value = item.get("value")
        if not isinstance(value, dict):
            continue
        for component in value.get("components") or []:
            if not isinstance(component, dict):
                continue
            ref = str(component.get("field_ref") or "").strip()
            if not ref:
                continue
            # ``field_ref`` 可能是 ``db.table.column`` —— 取最后一段。
            columns.append(ref.split(".")[-1].strip("`"))
    return tuple(dict.fromkeys(columns))


#: INSERT 的 ``FROM`` / ``JOIN`` 后面跟的表引用。
_SOURCE_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([`\w.]+)",
    re.IGNORECASE,
)


def extract_insert_source_tables(sql: str) -> tuple[str, ...]:
    """从 INSERT SQL 里解析出**源表**清单（回源比对用）。

    解析 ``FROM`` / ``JOIN`` 后面的表引用，去掉反引号、去掉 INSERT 的目标表。
    解析不出来时返回空元组 —— 回源比对据此降级为「无法验证」。

    **CTE 别名必须排除** —— ``WITH _src AS (...)`` 里的 ``_src`` 是
    **中间结果集**，不是物理表。把它当源表会让回源比对去查一张不存在的表
    → 必然 0 行 → 全部判成「无法验证」，而且还会浪费一次 semantic schema
    查询。

    调用方（``_run_quality_check_impl``）会先做静态 CTE 展开，正常情况下
    传进来的 SQL 已经不含 CTE；这里的排除是**兜底** —— 展开失败时（SQL 太
    复杂）至少不会把 CTE 别名当源表。
    """
    text = str(sql or "")
    if not text.strip():
        return ()
    # 去掉注释，避免注释里的 FROM 干扰。
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)

    target = _extract_insert_target_table_name(text)
    cte_aliases = {name.lower() for name in cte_names(text)}
    tables: list[str] = []
    for match in _SOURCE_TABLE_RE.finditer(text):
        name = match.group(1).strip().strip("`")
        if not name or name.lower() == "select":
            continue
        if target and name.lower() == target.lower():
            continue
        # CTE 别名不是物理表 —— 跳过。
        if name.lower() in cte_aliases:
            continue
        # 派生表别名（``FROM (SELECT ...) AS _src``）不是物理表 —— 跳过。
        if name.startswith("("):
            continue
        if name not in tables:
            tables.append(name)
    return tuple(tables)


def _extract_insert_target_table_name(sql: str) -> str:
    """从 INSERT 语句里取目标表名（纯文本解析，不做表名改写）。"""
    match = re.search(
        r"INSERT\s+(?:OVERWRITE|INTO)\s+TABLE\s+([`\w.]+)",
        sql,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return match.group(1).strip().strip("`")


def _fetch_semantic_primary_keys(
    *,
    target_table: str,
    real_target_table: str,
    direct_table: str | None,
    tool_context: Any,
) -> tuple[str, ...]:
    """从 semantic 元数据里取目标表的主键列。

    **查哪张表**：主键是**真实目标表**的属性 —— 临时表（``adhoctemp.tmp_*``）
    是我们自己在 ``adhoctemp`` 库里建的，没有 semantic 元数据。所以直连模式
    查 ``direct_table``，临时表模式查 ``real_target_table``。

    绝不抛异常 —— 查不到就降级成「没有主键」，让调用方跳过主键相关校验并
    注明原因，绝不让整轮校验崩掉。
    """
    lookup_table = (direct_table or "").strip() or (real_target_table or "").strip()
    if not lookup_table or lookup_table.startswith("adhoctemp."):
        # 临时表 / 裸表名 —— semantic 查不到，返回空。
        return ()
    try:
        from dataagent.actions.tools.local_tool.dataops_table_schema import (
            fetch_semantic_primary_key_columns,
        )

        return fetch_semantic_primary_key_columns(
            lookup_table,
            _tool_context=tool_context,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[quality_check] semantic primary key lookup failed: {exc!r}")
        return ()


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
    primary_key_columns: tuple[str, ...] = (),
    source_tables: tuple[str, ...] = (),
    source_sql: str = "",
    column_comments: dict[str, str] | None = None,
    count_fields: frozenset[str] | None = None,
    count_field_patterns: tuple[str, ...] | None = None,
    negative_exclude_patterns: tuple[str, ...] | None = None,
) -> list[tuple[str, str]]:
    """生成**每个 checker 一条**校验 SQL。

    Returns:
        ``[(checker_name, sql), ...]`` —— 逐条执行即可。

    每个 checker 的 SQL 内部用条件聚合一次算完它负责的所有字段，
    **不做**「checker × 字段」的 UNION ALL 展开。

    ``partition_filter`` 显式给出分区过滤条件（形如 ``pt_d = '20261020'``），
    用于**直连真实目标表**的模式；为 ``None`` 时用 ``partition_col`` +
    ``partition_date`` 拼（临时表模式，临时表只写了一个分区）。

    **表级 checker**（``scope == "table"``，如数据行数）不依赖字段 ——
    即使 ``fields`` 为空也会生成 SQL，所以这里的空字段短路只针对字段级 checker。
    """
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
        primary_key_columns=primary_key_columns,
        source_tables=source_tables,
        source_sql=source_sql,
        column_comments=column_comments,
        count_fields=count_fields,
        count_field_patterns=count_field_patterns,
        negative_exclude_patterns=negative_exclude_patterns,
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


def _detect_empty_table(
    *,
    merged: dict[str, Any],
    correctness_merged: dict[str, Any],
) -> CorrectnessViolation | None:
    """表内 0 行 → 判 error 级违规（阻断交付）。

    **为什么必须单独判**：0 行时字段级指标全是 ``0/0 = NULL`` → 渲染成 ``—``，
    看起来像"该指标不适用"。但真实语义是"**一行数据都没校验到**" —— 如果
    只靠 ``row_count`` 那条 warning，用户会把报告读成"只有 1 处小告警，其余
    字段都通过了"，而实际上整个校验是**空跑**。

    判定依据：``row_count`` 表级指标**成功算出且等于 0**。
    「没算出来」（checker 失败）**不算**空表 —— 那是「未知」，归 failed_checks
    管，不能猜成空表。

    **不猜行数**：只有真的跑出了 ``row_count = 0`` 才判空表。
    """
    table_metrics = merged.get("table_metrics") or {}
    if "row_count" not in table_metrics:
        return None
    row_count = _coerce_float(table_metrics.get("row_count"))
    if row_count is None or row_count != 0:
        return None

    # 字段级指标确实算不出来（NULL）—— 这是「空跑」的证据。
    details = merged.get("details") or {}
    empty_fields = [
        field_name
        for field_name, metrics in details.items()
        if isinstance(metrics, dict) and metrics and all(v is None for v in metrics.values())
    ]

    return CorrectnessViolation(
        rule="empty_table",
        field="",
        severity=SEVERITY_ERROR,
        message=(
            "目标表 0 行 —— 字段级校验全部无意义（空值率 / 单一值比率等均为 0/0 = NULL），"
            "本次校验实际未校验到任何数据。"
            "常见原因：JOIN 条件写错导致无匹配、上游源表该分区未落盘、"
            "或分区过滤条件把数据全过滤掉了。"
        ),
        evidence={
            "row_count": 0,
            "unverifiable_fields": empty_fields,
            "field_count": len(details),
        },
        suggestion=(
            "先确认上游源表在试算分区（今天往前 7 天）是否有数据；"
            "再检查 JOIN 的关联键与分区过滤条件是否写对。"
            "**不要**在 0 行的情况下交付 —— 本次没有任何数据被校验过。"
        ),
        warning_text=(
            "[empty_table] 目标表 0 行 —— 字段级校验全部无意义"
            "（空值率 / 单一值比率等均为 0/0 = NULL），本次校验未校验到任何数据"
        ),
    )


def _table_metric_payload(
    checker: QualityChecker,
    row: dict[str, Any],
    value: float | None,
) -> dict[str, Any]:
    """构造一个表级 checker 的 ``table_metrics`` 载荷。

    除了 checker 自己的 metric_key，主键重复率还会**附带**重复行数与有效
    行数（``pk_duplicate_rows`` / ``pk_total_rows``）—— 用户要看的是
    「重复了多少行」，光给一个比率不够定位问题。这两个 key 是**附加**信息，
    不影响 ``merge_correctness_results`` 对主 metric_key 的取值。
    """
    payload: dict[str, Any] = {checker.metric_key: value}
    if checker.name == "pk_duplicate_rate":
        duplicate_rows = _coerce_float(row.get("pk_duplicate_rows"))
        total_rows = _coerce_float(row.get("pk_total_rows"))
        payload["pk_duplicate_rows"] = duplicate_rows
        payload["pk_total_rows"] = total_rows
    return payload


def parse_checker_result(
    result: dict[str, Any],
    checker: QualityChecker,
    fields: list[str],
    config: QualityCheckConfig | None = None,
    ctx: CheckContext | None = None,
) -> dict[str, Any]:
    """解析**单个 checker** 的结果行。

    新 SQL 里每个 checker 的结果行就是 ``{字段名: 指标值}``（列名 = 字段名），
    所以这里按字段名取值；旧的 ``null_rate__col1`` 形式也一并兼容。

    ``details[field][checker.metric_key]`` —— 与历史返回结构保持一致。

    **表级 checker**（``scope == "table"``，如数据行数）走另一条路：结果行里
    是 ``{列名: 值}``（如 ``{"row_count": 1234}``），不挂在字段上，而是写进
    ``table_metrics[checker.metric_key]``。此时 ``details`` 为空 dict。

    同时产出**结构化违规**（``violations``）—— 自修正要靠它才知道「哪条规则、
    哪个字段、证据是什么」。``warnings`` 由 violations 派生（见
    :meth:`CorrectnessViolation.to_warning_text`），两者永远一致。
    """
    cfg = config or resolve_config(None)

    row: dict[str, Any] = {}
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        row = data[0]

    # --- 表级指标（如数据行数 / 主键重复率）---
    if checker.scope == _SCOPE_TABLE:
        value = _coerce_float(row.get(checker.metric_key))
        if value is None:
            # 兼容别名写法（如 ``row_count__total``）。
            value = _coerce_float(row.get(checker.alias("total")))
        if value is None and checker.name == "pk_duplicate_rate":
            # **兜底推导**：有些 adHoc 引擎只返回聚合出来的原始列
            # （``pk_duplicate_rows`` / ``pk_total_rows``），不返回比率列。
            # 这时在 Python 侧算比率 —— 口径与 SQL 完全一致
            # （重复行数 / 有效行数），比查不到比率就报「未完成」好得多。
            dup_rows = _coerce_float(row.get("pk_duplicate_rows"))
            total_rows = _coerce_float(row.get("pk_total_rows"))
            if dup_rows is not None and total_rows is not None:
                value = 0.0 if total_rows <= 0 else dup_rows / total_rows
        warnings: list[str] = []
        violations: list[CorrectnessViolation] = []
        if value is not None:
            threshold = cfg.threshold_for(checker)
            if checker.violates(value, threshold):
                text = checker.format_warning("table", value, threshold)
                warnings.append(text)
                violations.append(
                    CorrectnessViolation(
                        rule=checker.name,
                        field=_table_violation_field(checker),
                        severity=checker.severity,
                        message=(
                            f"重复率 {value:.6f} 超过阈值 {threshold:.6f}"
                            if checker.name == "pk_duplicate_rate"
                            else text
                        ),
                        evidence=_table_violation_evidence(checker, row, value),
                        suggestion=_table_violation_suggestion(checker),
                        warning_text=text,
                    )
                )
        return {
            "ok": len(warnings) == 0,
            "warnings": warnings,
            "violations": violations,
            "details": {},
            "table_metrics": _table_metric_payload(checker, row, value),
        }

    details: dict[str, dict[str, Any]] = {}
    warnings = []
    violations = []

    for field_name in fields:
        if not checker.applies_to_field_ctx(field_name, cfg, ctx):
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
            text = checker.format_warning(field_name, value, threshold)
            warnings.append(text)
            violations.append(
                CorrectnessViolation(
                    rule=checker.name,
                    field=field_name,
                    severity=checker.severity,
                    message=(
                        f"负值占比 {value:.6f} 超过阈值 {threshold:.6f}"
                        if checker.name == "negative_count"
                        else text
                    ),
                    evidence=_field_violation_evidence(checker, row, field_name, value),
                    suggestion=_field_violation_suggestion(checker, field_name),
                    warning_text=text,
                )
            )

    return {
        "ok": len(warnings) == 0,
        "warnings": warnings,
        "violations": violations,
        "details": details,
    }


def _table_violation_field(checker: QualityChecker) -> str:
    """表级违规的「字段」描述 —— 主键组合（而不是随便写个 ``*``）。"""
    if checker.name == "pk_duplicate_rate":
        return "主键"
    return ""


def _table_violation_evidence(
    checker: QualityChecker,
    row: dict[str, Any],
    value: float,
) -> dict[str, Any]:
    """表级违规的证据。"""
    if checker.name == "pk_duplicate_rate":
        return {
            "pk_duplicate_rate": value,
            "pk_duplicate_rows": _coerce_float(row.get("pk_duplicate_rows")),
            "pk_total_rows": _coerce_float(row.get("pk_total_rows")),
        }
    return {checker.metric_key: value}


def _table_violation_suggestion(checker: QualityChecker) -> str:
    """表级违规的修正方向（喂给模型）。"""
    if checker.name == "pk_duplicate_rate":
        return (
            "检查主键口径：是否漏了去重、JOIN 造成行放大、"
            "或主键本身不足以唯一标识一行（应补充缺失的主键列）。"
        )
    return ""


def _field_violation_evidence(
    checker: QualityChecker,
    row: dict[str, Any],
    field_name: str,
    value: float,
) -> dict[str, Any]:
    """字段级违规的证据。"""
    evidence: dict[str, Any] = {checker.metric_key: value}
    if checker.name == "negative_count":
        evidence["negative_rows"] = _coerce_float(row.get(f"neg_rows__{field_name}"))
    return evidence


def _field_violation_suggestion(checker: QualityChecker, field_name: str) -> str:
    """字段级违规的修正方向（喂给模型）。"""
    if checker.name == "negative_count":
        return (
            f"检查 `{field_name}` 的口径：是否把差值/变化量当成了计数、"
            "或聚合时漏了非负约束（应过滤掉负值后再计数）。"
        )
    return ""


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
    ctx: CheckContext | None = None,
) -> dict[str, Any]:
    """把多个 checker 的解析结果合并成一份 ``{ok, warnings, details}``。

    每个 checker 一条 SQL → 一份解析结果；这里按字段把各 checker 的指标
    拼到同一个 ``details[field]`` 里，warnings 顺次累加。

    ``details`` 的形状是**稳定的**：每个字段都带上全部生效 checker 的 metric key。
    某个 checker 没跑（比如没有比率类字段，异常值占比那条 SQL 压根没生成）
    或该字段不适用时，对应的值是 ``None`` —— 而不是缺这个 key。

    **表级 checker**（``scope == "table"``，如数据行数）的指标不进 ``details``，
    而是合并进 ``table_metrics``（``{metric_key: value}``）。

    ``violations`` 是**结构化违规**的并集 —— 自修正闭环的输入。
    """
    cfg = config or resolve_config(None)
    active = tuple(checkers) if checkers is not None else cfg.checkers
    by_name = {checker.name: checker for checker in active}

    field_scoped = [c for c in active if c.scope != _SCOPE_TABLE]
    table_scoped = [c for c in active if c.scope == _SCOPE_TABLE]
    details: dict[str, dict[str, Any]] = {
        name: {checker.metric_key: None for checker in field_scoped} for name in fields
    }
    table_metrics: dict[str, Any] = {checker.metric_key: None for checker in table_scoped}
    warnings: list[str] = []
    violations: list[CorrectnessViolation] = []

    for checker_name, parsed in parsed_results:
        checker = by_name.get(checker_name)
        metric_key = checker.metric_key if checker is not None else checker_name
        violations.extend(parsed.get("violations") or [])
        if checker is not None and checker.scope == _SCOPE_TABLE:
            value = (parsed.get("table_metrics") or {}).get(metric_key)
            table_metrics[metric_key] = value
            warnings.extend(parsed.get("warnings") or [])
            continue
        for field_name in fields:
            metrics = (parsed.get("details") or {}).get(field_name) or {}
            details.setdefault(field_name, {})[metric_key] = metrics.get(metric_key)
        warnings.extend(parsed.get("warnings") or [])

    return {
        "ok": len(warnings) == 0,
        "warnings": warnings,
        "violations": violations,
        "details": details,
        "table_metrics": table_metrics,
    }


def merge_correctness_results(
    parsed_results: Sequence[tuple[str, dict[str, Any]]],
    fields: list[str],
    config: QualityCheckConfig | None = None,
    checkers: Sequence[QualityChecker] | None = None,
    ctx: CheckContext | None = None,
) -> dict[str, Any]:
    """合并**正确性** checker 的解析结果。

    与 :func:`merge_checker_results` 的区别：

    - 正确性 checker 的**适用性是动态的** —— 没有主键就没有主键重复率，
      没有计数型字段就没有负值校验。不适用的 checker **完全不进入**结果，
      而不是留一个 ``None`` 占位（那读起来像「查了没查出来」）。
    - 结果只进 ``table_metrics`` / ``violations``，**不污染** ``details``
      （``details`` 的形状是历史契约，不能因为新增校验项而变）。

    Returns:
        ``{ok, warnings, violations, table_metrics, metrics, skipped}``。
        ``metrics`` 是字段级正确性指标（``{metric_key__字段名: 值}``）——
        与历史 ``details`` 分开，避免污染「details 的形状是稳定的」这条契约。
        ``skipped`` 记录**因信息不足而跳过**的校验项（不是失败）。
    """
    cfg = config or resolve_config(None)
    active = tuple(checkers) if checkers is not None else cfg.correctness_checkers
    by_name = {checker.name: checker for checker in active}

    # 表级 checker **预填 None** —— 即使它执行失败/超时、或压根没产出结果，
    # 报告里的「表级指标」表也必须有一行（例如「主键重复率 | 未完成」）。
    # 否则失败时该指标会**从报告里彻底消失**，用户看到的是「没这项校验」，
    # 而不是「这项校验跑了但失败」—— 这是两回事，也是最容易被误读成漏检的坑。
    table_metrics: dict[str, Any] = {
        checker.metric_key: None for checker in active if checker.scope == _SCOPE_TABLE
    }
    metrics: dict[str, Any] = {}
    warnings: list[str] = []
    violations: list[CorrectnessViolation] = []
    skipped: list[dict[str, str]] = []

    for checker_name, parsed in parsed_results:
        checker = by_name.get(checker_name)
        metric_key = checker.metric_key if checker is not None else checker_name
        violations.extend(parsed.get("violations") or [])
        warnings.extend(parsed.get("warnings") or [])
        if checker is not None and checker.scope == _SCOPE_TABLE:
            parsed_metrics = parsed.get("table_metrics") or {}
            value = parsed_metrics.get(metric_key)
            table_metrics[metric_key] = value
            # 主键重复率附带「重复行数 / 有效行数」—— 一起带进报告，
            # 用户才能看到「重复了多少行」而不只是一个比率。
            for extra_key in ("pk_duplicate_rows", "pk_total_rows"):
                if extra_key in parsed_metrics:
                    table_metrics[extra_key] = parsed_metrics[extra_key]
            continue
        for field_name in fields:
            field_metrics = (parsed.get("details") or {}).get(field_name) or {}
            value = field_metrics.get(metric_key)
            if value is None:
                continue
            metrics[f"{metric_key}__{field_name}"] = value

    return {
        "ok": len(warnings) == 0,
        "warnings": warnings,
        "violations": violations,
        "details": {},
        "table_metrics": table_metrics,
        "metrics": metrics,
        "skipped": skipped,
    }


# ============================================================================
# Markdown rendering (前端展示)
# ============================================================================
#
# 「数据质量校验」的结果要送到前端。两条链路都走这里，保证**表格形状一致**：
#   1. 数据校验 skill（``dataops_validate_data`` 工具）—— 直接返回表格；
#   2. 交付说明的渲染 —— 同一套 helper。
#
# 列：字段名 / 非空率(覆盖率) / 单一值比率 / 异常值占比 / 状态。
# ``None`` 表示该指标**不适用**（非比率类字段不参与异常值占比；或该 checker
# 没生成对应 SQL）—— 渲染成 ``—``，而不是 0（0 会被误读成「真的算出是 0」）。
#
# **表级指标**（数据行数）不进这张表 —— 它的列语义不同（不是比率），
# 单独用 :func:`render_table_metrics` 渲染成表级的一行。


def render_table_metrics(
    table_metrics: dict[str, Any] | None,
    truncated: bool = False,
) -> str:
    """把**表级指标**（如数据行数）渲染成一行 Markdown 表格。

    表级指标不是比率，语义上跟逐字段的比率表不同 —— 单独一张表更清楚，
    也避免把「10000 行」这种绝对值混进一列全是 0~1 比率的表里。

    Args:
        table_metrics: ``{metric_key: value | None}``。``None`` 表示该指标
            没算出来（执行失败/超时），渲染成 ``未完成``。
        truncated: 为 ``True`` 时注明行数只是**下界**（试算写入被截断过，
            真实数据可能更多）。

    Returns:
        Markdown 表格文本；无表级指标时返回空串（调用方按需拼接）。
    """
    if not table_metrics:
        return ""
    rows = []
    for metric_key, value in table_metrics.items():
        label = _TABLE_METRIC_LABELS.get(metric_key, metric_key)
        if value is None:
            cells = "未完成"
        elif float(value).is_integer():
            cells = str(int(value))
        else:
            cells = f"{float(value):.4f}"
        if truncated and metric_key == "row_count":
            label = f"{label}（≥，试算写入被截断）"
        rows.append(f"| {label} | {cells} |")
    header = "| 表级指标 | 值 |\n|---|---|"
    return "\n".join([header, *rows])


#: 表级 metric_key → 中文列名
_TABLE_METRIC_LABELS: dict[str, str] = {
    "row_count": "数据行数",
    "pk_duplicate_rate": "主键重复率",
    "pk_duplicate_rows": "主键重复行数",
    "pk_total_rows": "主键有效行数",
}


def render_metrics_table(
    details: dict[str, Any] | None,
    warnings: Sequence[str] | None = None,
    failed_metrics: dict[str, Sequence[str]] | None = None,
    empty_table: bool = False,
) -> str:
    """把质量校验的 ``details`` 渲染成 Markdown 表格（前端可直接渲染）。

    Args:
        details: ``{字段名: {metric_key: value | None}}``。``value`` 为 ``None``
            表示该指标不适用。
        warnings: 告警文案列表。用于给命中告警的字段标 ``⚠️ 告警``。
        failed_metrics: ``{字段名: [没算出来的 metric_key]}``。用于把「指标不适用」
            （渲染成 ``—``）和「指标没算出来」（渲染成 ``未完成``）区分开 —— 后者
            是执行失败/超时导致的缺口，不能读成「查过了，通过」。
        empty_table: 本轮表内 0 行。为 ``True`` 时，所有指标值渲染成
            **「无数据」**而不是 ``—`` —— ``—`` 读起来像「该指标不适用」，
            而空表下所有指标都是 ``0/0 = NULL``，真实语义是「**一行数据都
            没校验到**」。两者的区别就是「没查」和「不用查」。

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
            if empty_table:
                # 空表：所有字段级指标都是 0/0 = NULL —— 「无数据」而非「—」。
                return "无数据"
            return format_ratio(_metrics.get(metric_key))

        # 该字段有指标没算出来 → 状态列标「部分未完成」，不能标「通过」。
        if missing and field_name not in warned_fields:
            status = "⚠️ 部分未完成"
        elif empty_table and status != "⚠️ 告警":
            # 空表 → 状态列明说「无数据」，不能留「通过」或「—」。
            status = "⚠️ 无数据"

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


#: violation 的 rule → 中文列名。
_VIOLATION_RULE_LABELS: dict[str, str] = {
    "pk_duplicate_rate": "主键重复率",
    "negative_count": "计数型字段负值",
    "common_sense": "常识违规",
    "source_reconciliation": "回源比对",
    "empty_table": "空表（0 行）",
}

#: violation 的 severity → 展示文案。
_SEVERITY_LABELS: dict[str, str] = {
    SEVERITY_ERROR: "❌ 错误",
    SEVERITY_WARNING: "⚠️ 可疑",
}


def render_violations_table(
    violations: Sequence[CorrectnessViolation] | Sequence[dict[str, Any]] | None,
) -> str:
    """把**结构化违规**渲染成 Markdown 明细表。

    与 :func:`render_metrics_table` 并列 —— 后者渲染「指标表」（每字段一行），
    本函数渲染「违规表」（每条违规一行）。两者信息互补：指标表说「这个字段
    的覆盖率是多少」，违规表说「哪条规则、哪个字段、证据是什么、建议怎么改」。

    Args:
        violations: 结构化违规列表。可以是 :class:`CorrectnessViolation` 对象，
            也可以是它的 ``to_dict()`` 形式（跨进程传递后只剩 dict）。

    Returns:
        Markdown 表格文本；无违规时返回空串（调用方按需拼接）。
    """
    if not violations:
        return ""
    lines = [
        "| 规则 | 字段 | 级别 | 结论 | 证据 | 修正建议 |",
        "|---|---|---|---|---|---|",
    ]
    for raw in violations:
        # 兼容 dict 形态 —— 报告链路里违规常以序列化形式传递。
        item = raw if isinstance(raw, CorrectnessViolation) else CorrectnessViolation.from_dict(raw)
        lines.append(
            "| {rule} | {field} | {severity} | {message} | {evidence} | {suggestion} |".format(
                rule=_VIOLATION_RULE_LABELS.get(item.rule, item.rule),
                field=_escape_cell(item.field) if item.field else "—",
                severity=_SEVERITY_LABELS.get(item.severity, item.severity),
                message=_escape_cell(item.message),
                evidence=_escape_cell(_format_evidence(item.evidence)),
                suggestion=_escape_cell(item.suggestion or "—"),
            )
        )
    return "\n".join(lines)


def _escape_cell(text: Any) -> str:
    """把文本压成表格单元格（换行变空格、竖线转义）。"""
    value = str(text or "").replace("\r\n", " ").replace("\n", " ").replace("|", "\\|")
    return value.strip() or "—"


def render_violation_details(
    violations: Sequence[CorrectnessViolation] | Sequence[dict[str, Any]] | None,
    *,
    limit: int = 50,
) -> str:
    """把**每条违规的完整证据**渲染成逐条明细（不截断）。

    **为什么要有这个函数**：:func:`render_violations_table` 渲染的是宽表格，
    前端会把单元格里的长文本**截断**（显示成 ``…``）—— 用户看到的
    ``observed_value=英语,英语阅读,…,英语口语,英语口语;`` 其实是完整的，
    只是被折叠了。而「问题出在哪、查出来的数据是什么」正是用户排障时最需要
    的信息，不能被表格宽度吃掉。

    所以这里**再输出一次**同样内容的**纵向明细**：每条违规一个小节，
    结论 / 证据（逐个 key 一行） / 修正建议各占一行，不做任何省略。

    Args:
        violations: 结构化违规列表（对象或 ``to_dict()`` 形态均可）。
        limit: 最多渲染多少条 —— 防止极端情况下（成百上千条违规）把报告
            撑爆。超出的条数会在末尾注明。

    Returns:
        Markdown 明细文本；无违规时返回空串。
    """
    if not violations:
        return ""
    items = list(violations)
    shown = items[:limit]
    lines: list[str] = ["违规证据明细（完整展示，未截断）："]
    for index, raw in enumerate(shown, start=1):
        item = raw if isinstance(raw, CorrectnessViolation) else CorrectnessViolation.from_dict(raw)
        rule_label = _VIOLATION_RULE_LABELS.get(item.rule, item.rule)
        severity = _SEVERITY_LABELS.get(item.severity, item.severity)
        header = f"{index}. [{rule_label}]"
        if item.field:
            header += f" 字段 `{item.field}`"
        header += f" —— {severity}"
        lines.append(header)
        if item.message:
            lines.append(f"   - 结论：{item.message}")
        evidence = item.evidence if isinstance(item.evidence, dict) else {}
        if evidence:
            lines.append("   - 证据：")
            for key, value in evidence.items():
                if value is None:
                    continue
                if isinstance(value, float):
                    rendered = f"{value:.6f}"
                elif isinstance(value, (dict, list, tuple)):
                    rendered = _format_evidence_value(value)
                else:
                    rendered = str(value)
                lines.append(f"     - {key} = {rendered}")
        else:
            lines.append("   - 证据：（无）")
        if item.suggestion:
            lines.append(f"   - 修正建议：{item.suggestion}")
    remaining = len(items) - len(shown)
    if remaining > 0:
        lines.append(f"（另有 {remaining} 条违规未展示，可查看结构化结果中的 `violations` 字段。）")
    return "\n".join(lines)


def _format_evidence_value(value: Any) -> str:
    """把嵌套的证据值（dict / list）渲染成可读单行文本。"""
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


#: 流程型校验器的中文名（报告里用中文，不用内部英文 key）。
FLOW_CHECK_LABELS: dict[str, str] = {
    "common_sense": "常识违规检测",
    "source_reconciliation": "抽样回源比对",
}

#: 抽样明细最多渲染多少行 —— 防止极端情况下把报告撑爆。
_MAX_RENDERED_SAMPLE_ROWS = 20


def render_sample_data(notes: Any, *, title: str = "抽样数据明细") -> str:
    """渲染「抽样到的实际数据」。

    常识判定是**跨字段**的 —— 「年龄为 -5」单独看任何字段都正常，只有把
    同一行的多个取值摆在一起才看得出来。同理，「0 处违规」这个结论只有
    放回具体取值才可信：用户能自己核对「这 20 行到底长什么样」，才能判断
    「没违规」是真没问题，还是模型压根没认真看。

    没有违规明细的时候这一节尤其关键 —— 违规明细是「有问题才显示」，而
    抽样数据是「看了什么」，两者缺一不可。

    Args:
        notes: 某条流程型校验的 notes（含 ``sample_data`` 键）。
        title: 小节标题。

    Returns:
        Markdown 文本；没有抽样数据时返回空串。
    """
    if not isinstance(notes, dict):
        return ""
    rows = notes.get("sample_data")
    if not isinstance(rows, list) or not rows:
        return ""

    lines: list[str] = [f"{title}（共 {len(rows)} 行）："]
    for index, row in enumerate(rows[:_MAX_RENDERED_SAMPLE_ROWS], start=1):
        if not isinstance(row, dict):
            lines.append(f"- 第 {index} 行：{row!r}")
            continue
        cells = "，".join(f"{key}={_render_sample_value(value)}" for key, value in row.items())
        lines.append(f"- 第 {index} 行：{cells}")
    remaining = len(rows) - _MAX_RENDERED_SAMPLE_ROWS
    if remaining > 0:
        lines.append(f"- （另有 {remaining} 行未展示）")
    return "\n".join(lines)


def _render_sample_value(value: Any) -> str:
    """把一个抽样取值渲染成单行可读文本。"""
    if value is None:
        return "NULL"
    if isinstance(value, (dict, list, tuple)):
        return _format_evidence_value(value)
    return str(value)


def format_flow_note(notes: dict[str, Any]) -> str:
    """把一条流程型校验的 notes 压成一行可读文本。

    **「成功比对 N 行」必须带上实际比对字段数** —— ``matched_rows`` 只说明
    「这一行的回源查询成功且列名能对齐」，``compared_fields`` 才说明「真的
    拿几个字段值做了比对」。只报前者会被读成「N 行 × 全部字段都核对过」。

    Args:
        notes: ``{suffix: value}``（``status`` / ``sample_rows`` / ...）。
    """
    parts: list[str] = []
    status = str(notes.get("status") or "").strip()
    if status:
        parts.append(f"状态={status}")
    reason = str(notes.get("reason") or "").strip()
    if reason:
        parts.append(f"原因={reason}")
    for key, label in (
        ("sample_rows", "抽样 {value} 行"),
        ("violation_count", "发现 {value} 处违规"),
        ("matched_rows", "成功比对 {value} 行"),
    ):
        value = notes.get(key)
        if value is not None:
            parts.append(label.format(value=value))
    compared_fields = notes.get("compared_fields")
    if compared_fields is not None:
        parts.append(f"实际比对 {compared_fields} 个字段值")
    missing_fields = notes.get("missing_fields")
    if isinstance(missing_fields, list) and missing_fields:
        shown = ", ".join(str(item) for item in missing_fields[:8])
        parts.append(f"未覆盖字段 {len(missing_fields)} 个（{shown}）")
    unverifiable = notes.get("unverifiable")
    if isinstance(unverifiable, list) and unverifiable:
        parts.append(f"{len(unverifiable)} 行无法验证")
    return "；".join(parts) if parts else "已执行"


#: 明细摘要里每条流程型校验最多渲染几行抽样 —— 保证摘要整体够短。
_MAX_SUMMARY_SAMPLE_ROWS = 3

#: 明细摘要里每个字段值最多保留多少字符 —— 长值（如 JSON、逗号分隔串）
#: 不能把摘要撑爆，但也不能短到看不出是什么。
_MAX_SUMMARY_VALUE_CHARS = 40


def render_flow_details(flow_notes: Any) -> str:
    """把流程型校验的**明细**压成一段紧凑文本（可放进摘要区）。

    完整明细（:func:`render_sample_data` / :func:`render_reconcile_evidence`）
    动辄上千字，而终端渲染层会把工具结果截断到 500 字符
    （``DEFAULT_RICH_RESULT_TRUNCATION``）—— 完整明细排在报告末尾，永远看不到。

    实测：10 个字段的正常报告长 2412 字符，「流程型校验明细」小节从第
    686 字符才开始 —— 用户一行业务数据都看不到，只看到「抽样 20 行；发现
    0 处违规」这种没有内容支撑的结论句。

    这里给出的是**能塞进截断窗口的短版**：每条流程型校验带上「抽样了几行」
    「比对了哪几行、字段值是什么」「哪几行没比上、为什么」。结论句后面紧跟
    数据，用户不用翻文件就能判断「这轮到底验了什么」。

    Args:
        flow_notes: ``{checker_name: notes}``。

    Returns:
        紧凑的多行文本；没有可渲染内容时返回空串。
    """
    if not isinstance(flow_notes, dict) or not flow_notes:
        return ""

    lines: list[str] = []
    for name, notes in flow_notes.items():
        if not isinstance(notes, dict):
            continue
        label = FLOW_CHECK_LABELS.get(str(name), str(name))
        block = _render_flow_detail_block(label, notes)
        if block:
            # 块之间空行分隔 —— 否则上一块的「（另有 N 行未展示）」会和下一块
            # 的标题挤在同一行，Markdown 渲染时读成一坨。
            if lines:
                lines.append("")
            lines.extend(block)
    return "\n".join(lines)


def _render_flow_detail_block(label: str, notes: dict[str, Any]) -> list[str]:
    """渲染单条流程型校验的紧凑明细。"""
    lines: list[str] = []
    rows = notes.get("sample_data")
    if not isinstance(rows, list) or not rows:
        return lines

    # 回源比对：重点是「哪一行比上了什么」「哪一行没比上、为什么」。
    # 抽样行全部列出（最多 3 行）—— 因为「没比上的行」不在 ``comparisons``
    # 里，只列 ``comparisons`` 会漏掉它们。
    comparisons = notes.get("comparisons")
    unverifiable = notes.get("unverifiable")
    by_index: dict[Any, dict[str, Any]] = {}
    if isinstance(comparisons, list):
        for item in comparisons:
            if isinstance(item, dict):
                by_index[item.get("row_index")] = item
    reasons: dict[Any, str] = {}
    if isinstance(unverifiable, list):
        for item in unverifiable:
            if isinstance(item, dict):
                reasons[item.get("row_index")] = str(item.get("reason") or "无法验证")
            else:
                reasons[item.get("row_index")] = str(item)

    lines.append(f"{label}抽样明细（共 {len(rows)} 行）：")
    for index, row in enumerate(rows[:_MAX_SUMMARY_SAMPLE_ROWS], start=1):
        cells = _render_summary_row(row)
        detail = by_index.get(index)
        if detail is not None:
            compared = detail.get("compared_fields", 0)
            diff = _summarize_comparison(detail)
            lines.append(f"- 第 {index} 行：{cells} → 比对 {compared} 个字段{diff}")
        elif index in reasons:
            lines.append(f"- 第 {index} 行：{cells} → 未比对：{reasons[index]}")
        else:
            lines.append(f"- 第 {index} 行：{cells}")
    remaining = len(rows) - _MAX_SUMMARY_SAMPLE_ROWS
    if remaining > 0:
        lines.append(f"- （另有 {remaining} 行未展示）")
    return lines


def _render_summary_row(row: Any) -> str:
    """把一行抽样数据压成 ``k=v，k=v`` 形式（值超长则截断）。"""
    if not isinstance(row, dict):
        text = str(row)
        return text[:_MAX_SUMMARY_VALUE_CHARS]
    cells: list[str] = []
    for key, value in row.items():
        rendered = _render_sample_value(value)
        if len(rendered) > _MAX_SUMMARY_VALUE_CHARS:
            rendered = rendered[: _MAX_SUMMARY_VALUE_CHARS - 3] + "..."
        cells.append(f"{key}={rendered}")
    return "，".join(cells)


def _summarize_comparison(detail: dict[str, Any]) -> str:
    """把一行的比对结果压成短句（不一致的字段优先）。"""
    source_row = detail.get("source_row")
    feature_row = detail.get("feature_row")
    if not isinstance(source_row, dict) or not isinstance(feature_row, dict):
        return ""
    mismatched: list[str] = []
    matched = 0
    for field_name, source_value in source_row.items():
        if field_name not in feature_row:
            continue
        feature_value = feature_row[field_name]
        if _values_equal(feature_value, source_value):
            matched += 1
        else:
            mismatched.append(
                f"{field_name}（特征表={feature_value!r}≠源表={source_value!r}）"
            )
    parts: list[str] = []
    if matched:
        parts.append(f"一致 {matched} 个")
    if mismatched:
        parts.append("不一致：" + "，".join(mismatched[:3]))
    return "，" + "，".join(parts) if parts else ""


def render_reconcile_evidence(notes: Any) -> str:
    """渲染「抽样回源比对」的查询证据 —— 查了什么 SQL、查出了什么数据。

    回源比对是唯一能发现「口径算错但看起来合理」的校验手段，但它跑在
    「模型生成 SQL → 执行 → 比对」的链路上，中间任何一环降级（SQL 被白名单
    丢弃、列名对不上、查询返回 0 行）都可能被读成「通过」。把执行过的 SQL
    与实际取到的数据摊开，用户才能自己判断「这到底算不算真的回源核对了」。

    Args:
        notes: ``source_reconciliation`` 那份 notes。

    Returns:
        Markdown 明细文本；无 notes 时返回空串。
    """
    if not isinstance(notes, dict) or not notes:
        return ""

    lines: list[str] = ["回源比对查询证据："]

    executed = notes.get("executed_sql")
    if isinstance(executed, list) and executed:
        lines.append(f"- 已执行回源查询 {len(executed)} 条：")
        for item in executed[:10]:
            if not isinstance(item, dict):
                continue
            row_index = item.get("row_index")
            sql = str(item.get("sql") or "").strip()
            reason = str(item.get("reason") or "").strip()
            # SQL 压成一行 —— 逐行渲染会把截断窗口吃光，用户反而看不到后面
            # 的比对明细。折叠换行不丢内容，只在过长时截断并标注。
            if sql:
                one_line = " ".join(part.strip() for part in sql.splitlines() if part.strip())
                if len(one_line) > _MAX_SUMMARY_VALUE_CHARS * 3:
                    one_line = one_line[: _MAX_SUMMARY_VALUE_CHARS * 3 - 3] + "..."
                lines.append(f"  - 第 {row_index} 行 SQL：{one_line}")
            else:
                lines.append(f"  - 第 {row_index} 行：（无 SQL）")
            if reason:
                lines.append(f"    口径说明：{reason}")
    else:
        lines.append("- 未执行任何回源查询（没有生成可执行的只读 SQL）。")

    comparisons = notes.get("comparisons")
    if isinstance(comparisons, list) and comparisons:
        lines.append(f"- 实际比对明细（{len(comparisons)} 行）：")
        for item in comparisons[:10]:
            if not isinstance(item, dict):
                continue
            row_index = item.get("row_index")
            primary_key = item.get("primary_key")
            key_text = ""
            if isinstance(primary_key, dict) and primary_key:
                key_text = "，主键=" + ", ".join(f"{k}={v!r}" for k, v in primary_key.items())
            lines.append(
                f"  - 第 {row_index} 行：比对了 {item.get('compared_fields', 0)} 个字段{key_text}"
            )
            source_row = item.get("source_row")
            feature_row = item.get("feature_row")
            if isinstance(source_row, dict) and isinstance(feature_row, dict):
                for field_name, source_value in source_row.items():
                    if field_name not in feature_row:
                        continue
                    feature_value = feature_row[field_name]
                    same = "一致" if _values_equal(feature_value, source_value) else "**不一致**"
                    lines.append(
                        f"    - {field_name}：特征表={feature_value!r}；源表={source_value!r} → {same}"
                    )
            missing = item.get("missing_fields")
            if isinstance(missing, list) and missing:
                lines.append(f"    - 源表未返回列（无法比对）：{', '.join(str(m) for m in missing)}")

    unverifiable = notes.get("unverifiable")
    if isinstance(unverifiable, list) and unverifiable:
        lines.append(f"- 无法验证的行（{len(unverifiable)} 行）：")
        for item in unverifiable[:10]:
            if not isinstance(item, dict):
                lines.append(f"  - {item}")
                continue
            row_index = item.get("row_index")
            item_reason = str(item.get("reason") or "")
            lines.append(f"  - 第 {row_index} 行：{item_reason}")

    return "\n".join(lines) if len(lines) > 1 else ""


def _values_equal(left: Any, right: Any) -> bool:
    """两个取值是否一致（供报告里给「一致 / 不一致」标注）。"""
    try:
        from dataagent.actions.tools.local_tool.dataops_correctness_flow import values_match

        return values_match(left, right)
    except Exception:  # noqa: BLE001
        return str(left).strip() == str(right).strip()


def _format_evidence(evidence: dict[str, Any] | None) -> str:
    """把证据 dict 压成一行可读文本。"""
    if not evidence:
        return "—"
    parts: list[str] = []
    for key, value in evidence.items():
        if value is None:
            continue
        if isinstance(value, float):
            parts.append(f"{key}={value:.6f}")
        else:
            parts.append(f"{key}={value}")
    return "; ".join(parts) if parts else "—"

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
) -> tuple[str, str | None, dict[str, Any] | None, str]:
    """生产路径：execute_sql → get_query_result → collect。

    **失败时必须把 DataOps 的真实报错带出来** —— 否则报告里只剩一句
    ``status='error'``，排查的人根本不知道是「SQL 语法被拒」「表不存在」
    还是「引擎报错」。

    Returns:
        ``(status, job_id, result, error)`` —— ``error`` 是人类可读的失败原因
        （成功时为空串）。
    """
    from dataagent.actions.tools.local_tool.dataops_prod_adapter import (
        prod_collect_result,
        prod_execute_sql,
        prod_get_query_result,
    )

    # **提交前做 CTE 展开** —— 生产 adHoc 的表名解析阶段把 ``FROM _src``
    # 当成**物理表名**，直接报 ``table(s) agapdw._src do not exist``。
    #
    # 展开只作用在**提交给 DataOps 的这份 SQL**上 —— 交付物原文（落盘的
    # 文件）、``source_sql``（自修正底稿）、回源比对 prompt 拿到的都是
    # **未展开**的原貌。展开失败就带着 CTE 提交（保持原样，绝不产出
    # 半成品 SQL）—— 报错信息照样能看出是 CTE 问题。
    submit_sql = expand_ctes_static(check_sql)

    try:
        submit_result = await prod_execute_sql(submit_sql)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[quality_check] prod submit exception: {exc}")
        return "error", None, None, f"提交异常：{exc}"
    if not submit_result.get("success") or not submit_result.get("job_id"):
        reason = str(submit_result.get("error") or "submit failed").strip()
        logger.warning(f"[quality_check] prod submit failed: {reason}")
        return "error", None, None, reason
    job_id = submit_result["job_id"]

    deadline = asyncio.get_event_loop().time() + timeout_sec
    while asyncio.get_event_loop().time() < deadline:
        polled = await prod_get_query_result(job_id)
        status = polled.get("status")
        if status == "completed":
            collected = await prod_collect_result(job_id)
            if collected.get("status") != "completed":
                # collect 阶段的失败（引擎报错）同样要带出真实原因。
                reason = str(
                    collected.get("error") or collected.get("errorLog") or "collect failed"
                ).strip()
                return "failed", job_id, None, reason
            return "completed", job_id, collected, ""
        if status in {"failed", "error"}:
            reason = str(polled.get("error") or f"status={status!r}").strip()
            logger.warning(f"[quality_check] prod query failed: {reason}")
            return "failed", job_id, None, reason
        await asyncio.sleep(5.0)
    return "timed_out", job_id, None, f"超过 {timeout_sec}s 未返回结果"


async def _execute_check_sql_integration(
    check_sql: str,
    coordinator: Any,
    timeout_sec: int,
    poll_interval: float,
) -> tuple[str, str | None, dict[str, Any] | None, str]:
    """联调路径：submit_job → poll → collect。

    **失败时必须把 DataOps 的真实报错带出来** —— resource MCP 的
    ``submit_job`` / ``collect_job`` 都会把 ``error`` 字段带回来（里面是
    DataOps 的 ``errorMsg`` / Spark 报错），丢掉它报告里就只剩
    ``status='error'``，无从排查。

    Returns:
        ``(status, job_id, result, error)`` —— ``error`` 是人类可读的失败原因
        （成功时为空串）。
    """
    # **提交前做 CTE 展开** —— 与生产路径一致：只改**提交给 DataOps 的这份
    # SQL**，交付物原文与 ``source_sql`` 都不动。展开失败就带着 CTE 提交
    # （保持原样，绝不产出半成品 SQL）。
    submit_sql = expand_ctes_static(check_sql)

    try:
        submit_result = coordinator.submit_job(
            resource_id="dataops",
            command=submit_sql,
            task_type="sql_validate",
            timeout_sec=timeout_sec,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[quality_check] integration submit exception: {exc}")
        return "error", None, None, f"提交异常：{exc}"

    job_id = submit_result.get("job_id")
    if submit_result.get("status") == "ERROR" or not job_id:
        reason = str(
            submit_result.get("message")
            or submit_result.get("error")
            or "submit failed"
        ).strip()
        logger.warning(f"[quality_check] integration submit failed: {reason}")
        return "error", None, None, reason

    result_status, result = await _poll_until_done(coordinator, job_id, timeout_sec, poll_interval)
    if result_status != "completed" or not result:
        # ``result`` 里带着 DataOps 的 error 文本 —— 必须透传。
        reason = ""
        if isinstance(result, dict):
            reason = str(result.get("error") or result.get("summary") or "").strip()
        if not reason:
            reason = f"status={result_status!r}"
        logger.warning(f"[quality_check] integration check {result_status!r}: {reason}")
        return result_status, job_id, None, reason
    return "completed", job_id, result, ""


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
    row_limit: int = 0,
    fields: list[str] | None = None,
    column_types: dict[str, str] | None = None,
    unexcluded_fields: list[str] | None = None,
    semantic_primary_keys: Sequence[str] = (),
) -> dict[str, Any] | None:
    """对 INSERT 落地的表跑一轮数据正确性校验。

    **契约（两条链路共用，务必保持）：**
    - 只校验 INSERT 类 SQL；CREATE / SELECT / 空 SQL 一律返回 ``None``（跳过）。
      —— 唯一的例外：调用方**显式传了** ``fields``（独立数据验证从 DDL /
      semantic 层解析到的字段），此时不要求 SQL 是 INSERT。
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
        row_limit: 试算写入时的行数上限（> 0 表示临时表只写了前 N 行）。
            传这个值时，数据行数只是**下界**（真实数据可能更多），结果里会
            带上 ``row_count_truncated`` 标记，报告里也会注明。
        fields: **显式指定**要校验的字段。非空时用它替代从 SQL 解析的字段
            （独立数据验证从 DDL / semantic 层拿到字段的场景）。
        column_types: 字段类型映射（``{字段名: 小写类型}``）。决定判空写法 ——
            STRING 用 ``bicoredata.IsEmpty``，数值类用 ``IS NOT NULL``。
            为 ``None`` 时回退到从 workspace 的 DDL 里读。
        unexcluded_fields: **未排除**的字段全集（直连模式：semantic 元数据
            解析出来的原始列名）。用于**主键匹配** —— 排除名单里有 ``udid`` /
            ``id`` / ``uuid`` / ``guid``，而这些恰恰经常就是主键本身；拿
            「已排除」的字段去匹配主键会匹配不上 → 主键重复率整项被跳过。

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
            row_limit=row_limit,
            fields=fields,
            column_types=column_types,
            unexcluded_fields=unexcluded_fields,
            semantic_primary_keys=semantic_primary_keys,
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
    row_limit: int = 0,
    fields: list[str] | None = None,
    column_types: dict[str, str] | None = None,
    unexcluded_fields: list[str] | None = None,
    semantic_primary_keys: Sequence[str] = (),
) -> dict[str, Any] | None:
    if not config.enabled:
        logger.debug("[quality_check] disabled by config")
        return None
    # ``fields is not None`` 表示「调用方已经自己解析好字段了」（DDL / semantic /
    # DML 三条来源）—— 此时**不再要求 SQL 是 INSERT**。
    #
    # 注意：空的字段列表（``[]``）也算「显式指定」—— 只有表级校验项（数据行数）
    # 在启用时，字段一个都没有也应该能跑。
    explicit_fields = list(fields) if fields is not None else None
    if explicit_fields is None and not _is_insert_sql(sql or ""):
        return None

    # 注意：这里不做 CTE 展开。
    #
    # ``sql`` 是**交付物原文**（落盘的文件），它会流向三处：
    # ``source_sql``（自修正的增量修改底稿）、回源比对的 prompt（模型理解
    # 口径的依据）、字段/源表解析。这些都必须看到交付物的原貌 —— 展开后的
    # SQL 排版被重排（``PARTITION(pt_d=...)`` 少空格、多行折成一行），会让
    # 自修正在错误的基础上改。
    #
    # CTE 展开只发生在**提交给 DataOps 的那一步**（见
    # ``_execute_check_sql_prod`` / ``_execute_check_sql_integration``），
    # 以及回源比对生成即席 SQL 之后（见 ``dataops_source_reconciliation``）。
    # 那里提交的 SQL 是派生出来的，不是交付物本身。

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

    # 临时表模式下由「交付物 INSERT 的分区声明」推导出来的分区字面量。
    # 直连模式下用不到（那里查的是真实表的历史分区）。
    temp_partition_literals: dict[str, str] = {}

    # 直连模式：直接查真实目标表 —— 不建表、不插入、不用临时表替换。
    if direct_table:
        target_table = direct_table
        # 分区列名在下面按 DDL 解析（``resolved_partition_col``），这里先用
        # 配置的默认值占位；真正的 filter 在解析完分区列**之后**才拼。
        filter_expr = (partition_filter or "").strip()
    else:
        # 临时表名里嵌了同一个日期,所以推导目标表时必须用同一个 pt_date ——
        # 否则会拼出一个不存在的表名,校验静默返回空结果。
        if temp_table is None:
            temp_table = _extract_insert_target_table(sql, user_account, table_suffix, pt_date)
        if not temp_table:
            logger.debug("[quality_check] skipped: cannot extract temp table from sql")
            return None
        target_table = temp_table
        # **临时表模式的分区过滤：用这次 INSERT 实际写入的分区值。**
        #
        # 临时表是**分区表**（DDL 的 ``PARTITIONED BY`` 被原样保留），
        # Spark / Hive 的分析器会拒绝扫描分区表却没有分区条件的语句
        # （``partition condition not found in where or on node``）。
        # 所以临时表模式**不能简单地「不过滤」**。
        #
        # 但也**不能**用拼出来的基准日去过滤：临时表里只有这一次 INSERT
        # 写进去的数据，它落在哪个分区取决于**交付物自己的分区声明**：
        #
        #   INSERT OVERWRITE TABLE t PARTITION (pt_d = '20261020')  ← 数据在 20261020
        #   INSERT OVERWRITE TABLE t PARTITION (pt_d)                ← 动态分区，值来自 SELECT
        #
        # 用基准日（today-7）去过滤 → COUNT(1) 恒为 0 → 报「空表」→
        # 一次**成功的**试算被误判成「一行数据都没校验过」。
        #
        # 所以：**解析交付物 INSERT 里静态写死的分区字面量**，用它当过滤条件 ——
        # 口径与数据完全一致，既满足分区裁剪，又能查到行。
        # 真正的条件在下面「分区列解析」之后拼（那时才知道 DDL 声明的分区列）。
        temp_partition_literals = derive_insert_partition_literals(sql, pt_date)

    checkers = config.checkers
    # 字段来源：显式传入（DDL / semantic 层解析）优先于从 INSERT 投影解析。
    if explicit_fields is not None:
        resolved_fields = list(explicit_fields)
        # 显式传入的字段已经是最终口径 —— 不做二次排除。
        #
        # **主键匹配用「未排除」的字段集** —— 与试算链路同一口径。直连模式
        # 传进来的 ``fields`` 是「已排除」的（``udid`` / ``id`` 这些被滤掉了），
        # 而它们恰恰经常就是主键本身 → 匹配不上 → 主键重复率整项被跳过，
        # 报告却写成「跳过」，读起来像「查过了，没问题」。
        projection_fields = list(unexcluded_fields) if unexcluded_fields is not None else list(explicit_fields)
    else:
        # 延迟导入避免循环依赖（dataops_table_schema → dataops_quality_check）。
        from dataagent.actions.tools.local_tool.dataops_table_schema import (
            exclude_columns,
        )

        projection_fields = _extract_fields_from_insert_sql(sql, frozenset())
        resolved_fields = exclude_columns(projection_fields, config.exclude_fields)
    # 表级 checker（数据行数）不依赖字段 —— 字段解析不出来时它仍然可以跑。
    if not resolved_fields and not any(c.scope == _SCOPE_TABLE for c in checkers):
        logger.debug("[quality_check] skipped: no checkable fields")
        return None

    # 字段类型决定判空写法（STRING 用 bicoredata.IsEmpty，数值等用 IS NOT NULL）。
    # 优先级：显式传入 > 交付的 DDL > 读不到（退回 IS NOT NULL）。
    #
    # **临时表模式要用「真实目标表」去找 DDL，不能用临时表名。**
    # 临时表名是 ``adhoctemp.tmp_{user}_{date}_{table}`` —— workspace 里的 DDL
    # 声明的却是真实表（``biads.b``），两者永远匹配不上 → 匹配失败 →
    # 退回「按 mtime 取最新」→ **可能拿到另一张表的 DDL** → 把 A 表的列类型
    # 套到 B 表上（STRING 被当成数值 → 判空口径直接错），分区列也认错。
    ddl_lookup_table = target_table
    if not direct_table:
        real_target = extract_insert_target_table(sql)
        if real_target:
            ddl_lookup_table = real_target
    ddl_sql = _find_ddl_in_workspace(runtime, ddl_lookup_table)
    if column_types is not None:
        resolved_column_types = dict(column_types)
        _, resolved_column_comments = parse_ddl_columns_with_comments(ddl_sql)
    elif direct_table:
        # 直连模式：workspace 里的 DDL **可能是另一张表的** —— 拿它的类型套到
        # 目标表的同名列上，会把 STRING 列判成数值（判空口径直接错）。
        # 所以只认「表名对得上」的那份 DDL。
        resolved_column_types = _column_types_for_table(runtime, direct_table)
        _, resolved_column_comments = parse_ddl_columns_with_comments(ddl_sql)
    else:
        # 临时表模式：临时表就是按 workspace 的 DDL 建的，那份 DDL 是权威来源。
        #
        # 但**仍然要按目标表名校验** —— 万一匹配不上（DDL 缺失 / 裸表名解析不出），
        # 宁可退回 ``IS NOT NULL``（对所有类型都成立），也不要把**别张表**的
        # 类型套上来（STRING 被当成数值 → 判空口径直接错）。
        resolved_column_types = _column_types_for_table(runtime, ddl_lookup_table)
        _, resolved_column_comments = parse_ddl_columns_with_comments(ddl_sql)

    # --- 分区列解析（**不能硬编码 pt_d**）--------------------------------
    #
    # 交付物按 ``ds`` / ``dt`` / ``p_date`` 分区是常态。分区列名错了，
    # 直连模式的分区过滤就查不到行 → 报「空表」。
    #
    # 优先级：DDL 里声明的分区列 > 配置的 ``partition_col``。
    resolved_partition_col = config.partition_col
    ddl_partition_cols = parse_ddl_partition_columns(ddl_sql)
    if ddl_partition_cols:
        resolved_partition_col = ddl_partition_cols[0]
        if len(ddl_partition_cols) > 1:
            logger.debug(
                "[quality_check] DDL declares multiple partition columns %s; using %s",
                ddl_partition_cols,
                resolved_partition_col,
            )
    elif direct_table and config.partition_col == _DEFAULT_PARTITION_COL:
        # 直连模式读不到 DDL 时，semantic 元数据不给分区列信息 ——
        # 保留配置的默认值，但**明确记一笔**，别让它静默变成假空表。
        logger.debug(
            "[quality_check] direct mode: no DDL for %s, falling back to partition col %s",
            direct_table,
            resolved_partition_col,
        )

    # --- 主键 / 源表解析（新增正确性校验的输入）--------------------------
    # 主键只认 TASKIR 的声明（identity.components / output_features.grain）
    # 或显式配置 —— 不从 INSERT 的 GROUP BY 反推。
    #
    # 主键匹配用「未排除」的投影列（``projection_fields``），不用
    # ``resolved_fields``：排除名单里有 ``udid`` / ``id`` / ``uuid`` /
    # ``guid`` —— 这些恰恰经常就是主键本身。先排除再匹配，主键列被滤掉 →
    # 匹配不上 → 主键重复率整项被跳过，需求里「主键重复率必须为 0」这条
    # 最硬的规则根本没跑，报告却写成「跳过」，读起来像「查过了，没问题」。
    #
    # 排除名单的本意是「这些字段做**覆盖率**统计没有业务含义」，不是「这些
    # 字段不能当主键」—— 两个语义必须分开。
    #
    # semantic 元数据兜底：独立数据校验链路（用户输入「执行数据校验」）没建
    # DataTaskIR → ``identity.components`` / ``output_features.grain`` 都拿不到
    # → 主键重复率与回源比对整项跳过。semantic-service 的列元数据里带着主键
    # 标记（``is_primary_key``），是物理表层面的权威声明。
    #
    # 只在前三个来源都拿不到时才去查 semantic（多一次 HTTP 调用）—— 试算链路
    # 里 IR 通常都在，没必要多花这一跳。
    semantic_primary_keys: tuple[str, ...] = tuple(semantic_primary_keys or ())
    primary_key_columns = resolve_primary_key_columns(runtime, projection_fields, config)
    if not primary_key_columns:
        if not semantic_primary_keys:
            semantic_primary_keys = _fetch_semantic_primary_keys(
                target_table=target_table,
                real_target_table=ddl_lookup_table,
                direct_table=direct_table,
                tool_context=_tool_context,
            )
        if semantic_primary_keys:
            primary_key_columns = resolve_primary_key_columns(
                runtime,
                projection_fields,
                config,
                semantic_primary_keys=semantic_primary_keys,
            )
            logger.debug(
                "[quality_check] primary key resolved from semantic metadata: %s",
                list(primary_key_columns),
            )
    source_tables = extract_insert_source_tables(sql)

    # --- 分区过滤条件 -----------------------------------------------------
    #
    # **直连模式**：真实表里有**历史分区**，必须限定查哪一天；没给分区条件
    # 就用「解析出的分区列 + 一周前的日期」。
    #
    # **临时表模式**：用**交付物 INSERT 里静态写死的分区值**（见上面
    # ``temp_partition_literals``）。这是临时表**唯一**正确的口径：
    #
    # - 用基准日（today-7）→ 查不到行 → 报「空表」（假空表）；
    # - 完全不过滤 → 分区表扫描被 Spark/Hive 拒绝
    #   （``partition condition not found in where or on node``）。
    if direct_table:
        if not filter_expr:
            filter_expr = f"{resolved_partition_col} = '{pt_date}'"
    else:
        filter_expr = build_temp_table_partition_filter(
            temp_partition_literals,
            resolved_partition_col,
        )
        if filter_expr:
            logger.debug(
                "[quality_check] temp-table partition filter derived from INSERT: %s",
                filter_expr,
            )
        else:
            logger.debug(
                "[quality_check] temp-table partition filter: none "
                "(dynamic/unspecified partition in INSERT); scanning without filter"
            )

    # --- 组装统一的校验上下文 --------------------------------------------
    # 历史 checker 与正确性 checker **共用同一个上下文** —— 否则主键、列类型、
    # 计数型字段的判定会在「生成 SQL」与「解析结果」两个环节各算各的。
    ctx = build_check_context(
        temp_table=target_table,
        fields=resolved_fields,
        partition_col=resolved_partition_col,
        partition_date=pt_date,
        sample_size=config.sample_size,
        checkers=checkers,
        ratio_fields=config.ratio_fields,
        ratio_field_patterns=config.ratio_field_patterns,
        column_types=resolved_column_types,
        # **三态要原样传下去** —— ``""`` 表示「明确不过滤」（临时表模式），
        # ``None`` 表示「用默认条件」。写成 ``filter_expr or None`` 会把
        # ``""`` 变成 ``None`` → 又变回默认分区过滤 → 假空表。
        partition_filter=filter_expr,
        primary_key_columns=primary_key_columns,
        source_tables=source_tables,
        source_sql=sql,
        column_comments=resolved_column_comments,
        count_fields=config.count_fields,
        count_field_patterns=config.count_field_patterns,
        negative_exclude_patterns=config.negative_exclude_patterns,
    )
    statements = build_statements_from_context(ctx)

    # --- 正确性 checker 的 SQL（独立于历史 checker）------------------------
    correctness_ctx = CheckContext(
        **{
            **{f.name: getattr(ctx, f.name) for f in ctx.__dataclass_fields__.values()},
            "checkers": config.correctness_checkers,
        }
    )
    correctness_statements = build_statements_from_context(correctness_ctx)
    skipped_checks: list[dict[str, str]] = []
    for checker in config.correctness_checkers:
        if checker.name in {name for name, _ in correctness_statements}:
            continue
        # 不生成 SQL = **信息不足**，不是失败 —— 记 skipped，报告里注明。
        skipped_checks.append(
            {
                "name": checker.name,
                "reason": _skip_reason(
                    checker,
                    primary_key_columns,
                    resolved_fields,
                    ctx,
                    runtime=runtime,
                ),
            }
        )

    if not statements and not correctness_statements:
        return None

    check_timeout = min(timeout_sec, _MAX_CHECK_TIMEOUT_SEC)
    checker_by_name = {c.name: c for c in checkers}

    async def _run_one(checker_name: str, check_sql: str):
        """跑一条校验 SQL，返回 ``(checker_name, status, job_id, result, error)``。

        异常在这里就地捕获 —— 一条 checker 挂了不能把整个 ``gather`` 炸掉，
        否则连「是哪一条挂的」都拿不到。

        **兼容 3 元组与 4 元组返回** —— 执行层的返回值形状变了（多了
        ``error``），但既有单测与外部调用方可能还在返回/期望 3 元组。
        """
        try:
            if coordinator is None:
                outcome = await _execute_check_sql_prod(check_sql, check_timeout)
            else:
                outcome = await _execute_check_sql_integration(
                    check_sql, coordinator, check_timeout, poll_interval
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[quality_check] {checker_name} raised: {exc!r}")
            return checker_name, "error", None, None, f"{exc!r}"
        return (checker_name, *outcome)

    # 所有校验 SQL **并发**提交 —— DataOps 不支持把多个校验项合并成一条语句，
    # 顺序跑的话一条挂了后面全不跑；并发提交后各自独立轮询，互不影响。
    all_statements = [*statements, *correctness_statements]

    # --- 抽样数据（**每一项校验的「查出来的数据」都要进报告**）------------
    #
    # 为什么必须单独抽一次：确定性 checker（空值率 / 单一值比率 / 异常值占比）
    # 只返回**聚合值**，不带任何原始行 —— 报告里只有「非空率 0.9500」，
    # 用户看不到「这批数据长什么样」。而流程型校验（常识 / 回源比对）
    # 虽然抽样了，但那是**各自的抽样口径**（20 行 / 3 行，且列不同）。
    #
    # 这里抽一次**统一口径**的样本（主键列 + 全部校验字段），作为
    # 「本次校验查到的数据示例」—— 报告的「数据示例」小节渲染它。
    #
    # **与校验项并发跑** —— 抽样只是一条额外的 SELECT，不该让整轮校验
    # 多等一个 DataOps 往返。
    #
    # **抽不到不算失败** —— 抽样是「锦上添花」的证据，缺了只影响报告完整度，
    # 不影响任何校验结论。所以它**不**进 ``failed_checks``。
    sample_task = asyncio.create_task(
        _capture_sample_rows(
            execute=_sample_executor(coordinator, timeout_sec, poll_interval),
            target_table=target_table,
            fields=resolved_fields,
            primary_key_columns=primary_key_columns,
            partition_filter=filter_expr,
            limit=_SAMPLE_EXAMPLE_ROWS,
        )
    )
    outcomes = await asyncio.gather(
        *(_run_one(name, sql) for name, sql in all_statements),
        return_exceptions=True,
    )
    sample_rows_payload = await sample_task

    parsed_results: list[tuple[str, dict[str, Any]]] = []
    correctness_parsed: list[tuple[str, dict[str, Any]]] = []
    job_ids: list[str] = []
    failed_checks: list[dict[str, Any]] = []
    correctness_by_name = {c.name: c for c in config.correctness_checkers}
    for outcome in outcomes:
        if isinstance(outcome, BaseException):  # pragma: no cover - _run_one 已兜底
            logger.warning(f"[quality_check] check raised: {outcome!r}")
            continue
        # 执行层可能返回 3 元组（无 error 字段）或 4 元组（带 error 字段）。
        if len(outcome) == 5:
            checker_name, status, job_id, result, error_text = outcome
        else:
            checker_name, status, job_id, result = outcome
            error_text = ""
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
        if checker_name in correctness_by_name:
            correctness_parsed.append(
                (
                    checker_name,
                    parse_checker_result(
                        result,
                        correctness_by_name[checker_name],
                        resolved_fields,
                        config,
                        correctness_ctx,
                    ),
                )
            )
            continue
        checker = checker_by_name.get(checker_name)
        if checker is None:  # pragma: no cover - 理论上不会发生
            continue
        parsed_results.append(
            (checker_name, parse_checker_result(result, checker, resolved_fields, config, ctx))
        )

    if not parsed_results and not correctness_parsed:
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

    merged = merge_checker_results(parsed_results, resolved_fields, config, checkers)
    correctness_merged = merge_correctness_results(
        correctness_parsed, resolved_fields, config, config.correctness_checkers, correctness_ctx
    )

    # --- 空表守卫：0 行 → 字段级指标全部无意义 ---------------------------
    #
    # 表里 0 行时，字段级指标算出来是 ``SUM(IF(...)) / COUNT(1)`` = ``0/0`` =
    # **NULL**。解析层拿不到值 → ``details[field][metric] = None`` → 渲染成
    # ``—``（"该指标不适用"）。**这是错的**：不适用是"这个字段没有这种指标"，
    # 而空表是"一行数据都没校验到" —— 两者必须区分开。
    #
    # 更糟的是：空表下 ``row_count`` 会报一条 warning，但**字段级校验全被
    # 静默放过**，报告看起来"只有 1 处告警、其余都通过"，读成"数据没问题"。
    # 实际上一行都没验过。
    #
    # 所以：0 行 → 判 **error 级违规**，阻断交付（用户选定的语义）。
    empty_table_violation = _detect_empty_table(
        merged=merged,
        correctness_merged=correctness_merged,
    )

    # --- 流程型校验（常识违规 / 抽样回源比对）-----------------------------
    flow_result = await _run_flow_checks(
        sql=sql,
        target_table=target_table,
        fields=resolved_fields,
        primary_key_columns=primary_key_columns,
        source_tables=source_tables,
        column_types=resolved_column_types,
        column_comments=resolved_column_comments,
        # 直连模式：``filter_expr`` 已经有内容（显式指定或默认拼出来的）；
        # 临时表模式：``filter_expr`` 是 ``""`` —— 临时表里只有本次试算的
        # 数据，不需要分区过滤（过滤会查不到行 → 假空表）。
        partition_filter=filter_expr,
        config=config,
        runtime=runtime,
        tool_context=_tool_context,
        coordinator=coordinator,
        timeout_sec=timeout_sec,
        poll_interval=poll_interval,
    )

    # 失败的 checker 覆盖到的字段，其对应指标是「未知」而不是「不适用」——
    # 单独记一份，供渲染时把状态列标成「未完成」，避免读成「查过了，通过」。
    failed_metrics: dict[str, list[str]] = {}
    for item in failed_checks:
        checker = checker_by_name.get(item["name"])
        if checker is None:
            continue
        if checker.scope == _SCOPE_TABLE:
            # 表级指标不挂在字段上 —— 它失败就是「整表行数未知」。
            continue
        for field_name in resolved_fields:
            if checker.applies_to_field(field_name, config):
                failed_metrics.setdefault(field_name, []).append(checker.metric_key)

    failed_names = {str(item["name"]) for item in failed_checks}
    completed_checks = [name for name, _ in all_statements if name not in failed_names]

    # --- 汇总告警与违规 ---------------------------------------------------
    # ``warnings`` 由 violations 派生 —— 保证「告警文案」与「结构化违规」
    # 永远一致（每条告警都能追溯到唯一一条违规）。
    all_violations: list[CorrectnessViolation] = [
        *(merged.get("violations") or []),
        *(correctness_merged.get("violations") or []),
        *(flow_result.get("violations") or []),
    ]
    # 空表 → error 级违规（阻断交付）。放在最后 —— 它是**总括性**结论，
    # 应该排在具体字段违规之后。
    if empty_table_violation is not None:
        all_violations.append(empty_table_violation)
    warnings = [item.to_warning_text() for item in all_violations]
    # 流程型校验「未完成」不算违规，但它必须出现在报告里。
    incomplete_checks = [*failed_checks, *(flow_result.get("incomplete") or [])]
    for item in incomplete_checks:
        if not item.get("reason"):
            item["reason"] = item.get("status") or "未完成"

    return {
        # 有 checker 没跑完就不能说「全部通过」——但**已经跑出来的指标照常给结论**。
        "ok": not warnings and not incomplete_checks,
        "warnings": warnings,
        # 结构化违规 —— 自修正闭环的输入。
        "violations": [item.to_dict() for item in all_violations],
        "details": merged["details"],
        # 表级指标（如数据行数）—— 不在逐字段的 details 里，单独一层。
        "table_metrics": {
            **(merged.get("table_metrics") or {}),
            **(correctness_merged.get("table_metrics") or {}),
        },
        # 本轮表内 0 行 —— 字段级校验全是空跑，报告要据此改口径
        # （「无数据」而不是「不适用」），并已产出 error 级违规。
        "empty_table": empty_table_violation is not None,
        # 字段级正确性指标（不进 details —— details 的形状是历史契约）。
        "correctness_metrics": correctness_merged.get("metrics") or {},
        "sampled": True,
        "sample_size": config.sample_size,
        "job_id": job_ids[-1] if job_ids else None,
        "job_ids": job_ids,
        # 实际执行了的校验项（没有比率类字段时异常值占比那条 SQL 不会生成）。
        "checks": [name for name, _ in all_statements],
        # **正确性校验项**（主键重复率 / 计数型字段负值）—— 与「质量校验项」
        # 分开列，报告的「校验概况」小节要能说清「哪些是硬约束」。
        #
        # **含被跳过的** —— 「配了但没跑」（信息不足）与「配了且跑了」是
        # 两回事，概况里必须把前者也列出来，否则用户以为只查了跑成的那些。
        "correctness_checks": [
            name for name, _ in correctness_statements
        ] + [item["name"] for item in skipped_checks],
        # 跑成了的校验项（failed_checks 的补集）。
        "completed_checks": completed_checks,
        # 没跑成的校验项（超时 / 失败 / 抛异常），每项带原因。
        "failed_checks": incomplete_checks,
        # ``{字段名: [该字段上没算出来的 metric_key]}`` —— 用于渲染时区分
        # 「不适用」（None）和「没算出来」。
        "failed_metrics": failed_metrics,
        # 因信息不足而**跳过**的校验项（不是失败）。
        "skipped_checks": skipped_checks,
        # 流程型校验的旁路记录（「无法验证」的行等）。
        "flow_notes": flow_result.get("notes") or {},
        # **本次校验查到的数据示例**（统一口径抽样）—— 报告的「数据示例」
        # 小节渲染它。空列表表示抽样没成功（不影响任何校验结论）。
        "sample_rows": sample_rows_payload,
        # 本轮是部分完成（有的跑成了、有的没跑成）。
        "partial": bool(incomplete_checks),
        "temp_table": target_table,
        "partition_date": pt_date,
        # 直连模式额外标记：本轮校验的是真实目标表（没走临时表）。
        "direct": bool(direct_table),
        # 主键列（主键重复率校验用了哪几列）。
        "primary_key_columns": list(primary_key_columns),
        # 试算链路写入被截断（只写了前 N 行）—— 此时行数只是**下界**，
        # 真实数据可能更多。直连真实表时这个标记恒为 False。
        "row_count_truncated": bool(row_limit and row_limit > 0 and not direct_table),
        "row_limit": row_limit if row_limit and row_limit > 0 and not direct_table else None,
        # 主键重复率的口径标记：主键重复率**全表算，不采样** —— 但试算链路的
        # 临时表本身只有前 N 行（写入被截断），所以算的是「临时表这 N 行里的
        # 重复率」，不是全量数据的重复率。直连真实目标表时恒为 False。
        "pk_duplicate_truncated": bool(row_limit and row_limit > 0 and not direct_table),
    }


#: 报告里「数据示例」小节展示多少行 —— 多了报告会被撑爆，用户也读不完。
_SAMPLE_EXAMPLE_ROWS = 5


def _sample_executor(
    coordinator: Any,
    timeout_sec: int,
    poll_interval: float,
) -> Any:
    """造一个「执行一条只读 SELECT」的协程（供抽样用）。

    与 :func:`_run_flow_checks` 里的 ``_execute`` 同源 —— 差别只在返回值：
    这里的 ``rows`` 已经过滤成 ``list[dict]``，抽样不需要更多。

    **绝不抛异常** —— 抽样只是报告的「锦上添花」，失败就返回空。
    """

    async def _execute(statement: str) -> tuple[str, list[dict[str, Any]] | None]:
        try:
            if coordinator is None:
                outcome = await _execute_check_sql_prod(statement, timeout_sec)
            else:
                outcome = await _execute_check_sql_integration(
                    statement, coordinator, timeout_sec, poll_interval
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[quality_check] sample query failed: {exc!r}")
            return "error", None
        status, _job_id, payload = outcome[0], outcome[1], outcome[2]
        if status != "completed" or not payload:
            return status, None
        rows = payload.get("data")
        if not isinstance(rows, list):
            return "completed", []
        return "completed", [row for row in rows if isinstance(row, dict)]

    return _execute


async def _capture_sample_rows(
    *,
    execute: Any,
    target_table: str,
    fields: list[str],
    primary_key_columns: tuple[str, ...],
    partition_filter: str,
    limit: int = _SAMPLE_EXAMPLE_ROWS,
) -> list[dict[str, Any]]:
    """抽几行**原始数据**，供报告的「数据示例」小节展示。

    **为什么必须有**：确定性 checker 只返回**聚合值**（非空率 0.95），
    报告里没有任何一行业务数据 —— 用户无法判断「这批数据到底长什么样」，
    「校验通过」也就成了没有内容支撑的结论。

    **为什么抽列是「主键 + 全部校验字段」**：主键让用户能定位到具体记录；
    校验字段让用户能对照指标（哪些字段空、哪些字段值异常）。

    **失败不抛异常、不算校验失败** —— 返回空列表即可。
    """
    if execute is None or not target_table:
        return []
    columns = list(primary_key_columns)
    for name in fields:
        if name not in columns:
            columns.append(name)
    if not columns:
        return []
    try:
        from dataagent.actions.tools.local_tool.dataops_correctness_flow import (
            build_sample_sql,
            record_sample_rows,
        )

        sql = build_sample_sql(
            target_table,
            columns,
            partition_filter=partition_filter,
            limit=limit,
        )
        if not sql:
            return []
        status, rows = await execute(sql)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[quality_check] sample capture failed: {exc!r}")
        return []
    if status != "completed" or not rows:
        return []
    return record_sample_rows(list(rows), limit=limit)


async def _run_flow_checks(
    *,
    sql: str,
    target_table: str,
    fields: list[str],
    primary_key_columns: tuple[str, ...],
    source_tables: tuple[str, ...],
    column_types: dict[str, str],
    column_comments: dict[str, str],
    partition_filter: str,
    config: QualityCheckConfig,
    runtime: Any,
    tool_context: Any,
    coordinator: Any,
    timeout_sec: int,
    poll_interval: float,
) -> dict[str, Any]:
    """跑**流程型**校验（常识违规 / 抽样回源比对）。

    与确定性 checker 的区别：它们不是「一条 SQL 一个指标」，而是多步骤流程
    （抽样 → 模型判定 → 生成即席 SQL → 执行 → 比对）。所以走独立抽象
    :class:`~dataops_correctness_flow.FlowChecker`。

    **绝不抛异常** —— 一条流程型校验挂了只记「未完成」，不影响其它结果，
    也不让整轮校验崩掉。
    """
    result: dict[str, Any] = {"violations": [], "incomplete": [], "notes": {}}
    try:
        from dataagent.actions.tools.local_tool.dataops_correctness_flow import (
            FlowContext,
            resolve_llm,
        )
        from dataagent.actions.tools.local_tool.dataops_flow_checkers import (
            build_flow_checkers,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[quality_check] flow checkers unavailable: {exc!r}")
        result["incomplete"].append(
            {"name": "flow_checks", "status": "unavailable", "reason": f"模块不可用：{exc}"}
        )
        return result

    checkers = build_flow_checkers()
    if not checkers:
        return result

    llm = resolve_llm(runtime)
    if llm is None:
        # 模型不可用 → 两条流程型校验都无法判定 —— 记「未完成」，不猜。
        result["incomplete"].append(
            {"name": "flow_checks", "status": "unavailable", "reason": "模型不可用"}
        )
        return result

    async def _execute(statement: str) -> tuple[str, list[dict[str, Any]] | None]:
        """执行一条只读 SELECT，返回 ``(status, rows)``。"""
        try:
            if coordinator is None:
                outcome = await _execute_check_sql_prod(statement, timeout_sec)
            else:
                outcome = await _execute_check_sql_integration(
                    statement, coordinator, timeout_sec, poll_interval
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[quality_check] flow query failed: {exc!r}")
            return "error", None
        # 兼容 3 元组（无 error 字段）与 4 元组（带 error 字段）。
        status, _job_id, payload = outcome[0], outcome[1], outcome[2]
        if status != "completed" or not payload:
            return status, None
        rows = payload.get("data")
        if not isinstance(rows, list):
            return "completed", []
        return "completed", [row for row in rows if isinstance(row, dict)]

    flow_ctx = FlowContext(
        sql=sql,
        target_table=target_table,
        fields=list(fields),
        primary_key_columns=primary_key_columns,
        source_tables=source_tables,
        column_types=column_types,
        column_comments=column_comments,
        partition_filter=partition_filter,
        common_sense_sample_size=config.common_sense_sample_size,
        reconcile_sample_size=config.reconcile_sample_size,
        relative_tolerance=config.relative_tolerance,
        absolute_tolerance=config.absolute_tolerance,
        execute_sql=_execute,
        llm=llm,
        llm_kwargs=_flow_llm_kwargs(llm),
        tool_context=tool_context,
    )

    for checker in checkers:
        try:
            violations = await checker.run(flow_ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[quality_check] flow checker {checker.name} failed: {exc!r}")
            result["incomplete"].append(
                {"name": checker.name, "status": "failed", "reason": f"{exc!r}"}
            )
            continue
        result["violations"].extend(violations or [])
        # **收集该 checker 的全部 notes** —— notes 的 key 是
        # ``<checker_name>__<suffix>``（如 ``common_sense__status``），
        # 早先这里只取 ``ctx.notes.get(checker.name)``（裸名），永远取不到，
        # 于是 ``flow_notes`` 恒为空、报告里看不到流程型校验的任何痕迹。
        #
        # 注意历史上分隔符有两种写法（``common_sense__status`` 与
        # ``common_sense_sample_rows``）—— 两种都要认，否则会漏掉
        # 「抽样了几行」「比对了几行」这些旁路记录。
        checker_notes: dict[str, Any] = {}
        for key, value in flow_ctx.notes.items():
            if key.startswith(f"{checker.name}__"):
                checker_notes[key[len(checker.name) + 2 :]] = value
            elif key.startswith(f"{checker.name}_"):
                checker_notes[key[len(checker.name) + 1 :]] = value
            elif key == checker.name and isinstance(value, dict):
                checker_notes.update(value)
        if checker_notes:
            result["notes"][checker.name] = checker_notes
        status = flow_ctx.notes.get(f"{checker.name}__status")
        if status and status != "completed":
            result["incomplete"].append(
                {
                    "name": checker.name,
                    "status": status,
                    "reason": str(flow_ctx.notes.get(f"{checker.name}__reason") or status),
                }
            )
    return result


def _flow_llm_kwargs(llm: Any) -> dict[str, Any]:
    """流程型校验的模型调用参数（关闭思考 —— 判定与生成都不需要深思）。"""
    try:
        from dataagent.core.managers.llm_manager.thinking import validation_llm_kwargs

        return validation_llm_kwargs(llm, enable_thinking=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[quality_check] validation_llm_kwargs unavailable: {exc!r}")
        return {}


def _skip_reason(
    checker: QualityChecker,
    primary_key_columns: tuple[str, ...],
    fields: Sequence[str],
    ctx: CheckContext,
    *,
    runtime: Any = None,
) -> str:
    """说明**为什么**这项校验没跑。

    **文案必须指向真正的责任方** —— 早先这里一律写「未提供主键列
    （…与配置均未提供）」，把责任推给「需要人工补主键配置」。生产实测
    那次并不是配置缺失：DataTaskIR 里**已经有**主键声明（``identity
    .components`` = ``app_type`` + ``conversion_target``），只是校验链路
    **没读到**它 —— 读的是 ``Runtime`` 的进程内缓存，而缓存只在触发
    IR post-hook 的那一轮才有值。

    所以要区分两种情况：**声明存在但读不到**（链路问题）/ **声明确实
    不存在**（配置问题）。
    """
    if checker.name == "pk_duplicate_rate":
        if not primary_key_columns:
            declared = _primary_key_from_ir(runtime) or _primary_key_from_ir_grain(runtime)
            if declared:
                # 声明有，链路没读到 —— 这是**链路问题**，不是配置问题。
                return (
                    "DataTaskIR 已声明主键（"
                    + " + ".join(declared)
                    + "），但校验链路未读到该口径"
                    "（主键声明与目标表字段未能整组匹配上）"
                )
            return (
                "未找到主键声明（DataTaskIR 的 identity.components / "
                "output_features.grain、配置与 semantic 元数据均未提供）"
            )
        return "主键列不在目标表的字段里"
    if checker.name == "negative_count":
        count_fields, unknown = resolve_count_fields(
            fields,
            ctx.column_types,
            count_fields=ctx.count_fields,
            count_field_patterns=ctx.count_field_patterns,
            negative_exclude_patterns=ctx.negative_exclude_patterns,
        )
        if count_fields:
            return "字段类型不可判定"
        if unknown:
            return f"字段类型未知（{', '.join(unknown)}）"
        return "没有计数型字段"
    return "信息不足"


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
