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
"""流程型数据正确性校验的共享基座（FlowChecker）。

:mod:`dataops_quality_check` 里的 ``QualityChecker`` 假设「一个指标 = 一条 SQL +
一个阈值」，但新增的两条规则不满足这个假设：

- **常识违规**：要看同一行上多个字段的联合语义，必须由模型判定；
- **抽样回源比对**：抽样 → 逐行生成即席 SQL → 执行源表查询 → 逐字段比对，
  是一条多步骤流程。

所以这里另起一套并列概念：**FlowChecker** —— 一个 ``async run(ctx) -> list[violation]``。

依赖方向：本模块只依赖 :mod:`dataops_quality_check` 的类型定义，而后者只在
**函数体内**延迟导入本模块 —— 避免循环导入。
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from dataagent.actions.tools.local_tool.dataops_quality_check import CorrectnessViolation
from dataagent.actions.tools.local_tool.dataops_sql_identifiers import (
    UnsafeIdentifierError,
    quote_column,
    quote_table,
    validate_partition_filter,
)
from dataagent.utils.log import logger

# ============================================================================
# Context / Checker abstraction
# ============================================================================

#: 执行一条任意 SELECT，返回 ``(status, rows)``。
#: ``status`` 取 ``"completed"`` / ``"failed"`` / ``"timed_out"`` / ``"error"``；
#: ``rows`` 是 ``list[dict]``（列名 → 取值），失败时为 ``None``。
SqlExecutor = Callable[[str], Awaitable[tuple[str, list[dict[str, Any]] | None]]]


@dataclass
class FlowContext:
    """一次流程型校验所需的全部输入。

    与 :class:`~dataops_quality_check.CheckContext` 并列：后者服务于
    「一条 SQL 一个指标」的确定性校验，前者服务于多步骤流程。
    """
    #: 原始 INSERT SQL（回源比对用它理解口径，自修正用它做增量修改的底稿）。
    sql: str
    #: 被校验的表（临时表链路是 ``adhoctemp.tmp_*``；直连链路是真实目标表）。
    target_table: str
    #: 被校验的字段（目标表里真实存在的列名）。
    fields: list[str] = field(default_factory=list)
    #: 主键列（空表示本轮没有主键信息 —— 相关校验降级为「无法验证」）。
    primary_key_columns: tuple[str, ...] = ()
    #: 从 INSERT SQL 解析出的源表清单。
    source_tables: tuple[str, ...] = ()
    #: 字段类型（``{字段名: 小写类型}``）。
    column_types: dict[str, str] = field(default_factory=dict)
    #: 字段业务注释（``{字段名: comment}``）—— 常识判定靠它给出违反依据。
    column_comments: dict[str, str] = field(default_factory=dict)
    #: 分区过滤条件（与其它校验项同一口径）。
    partition_filter: str = ""
    #: 常识检测抽样行数。
    common_sense_sample_size: int = 20
    #: 回源比对抽样行数。
    reconcile_sample_size: int = 3
    #: 数值相对容差 / 绝对容差。
    relative_tolerance: float = 1e-6
    absolute_tolerance: float = 1e-9
    #: 执行任意 SELECT 的入口。
    execute_sql: SqlExecutor | None = None
    #: 模型实例（``None`` 表示模型不可用 → 相关校验降级为「未完成」）。
    llm: Any = None
    #: 模型调用的 per-call kwargs（思考开关等）。
    llm_kwargs: dict[str, Any] = field(default_factory=dict)
    #: 工具执行上下文（回源比对要用它查源表 schema）。
    tool_context: Any = None
    #: 流程型校验器的旁路记录（「无法验证」的行、采样情况等）——
    #: 这些不是违规，但必须出现在报告里，否则用户会以为「查过了、没问题」。
    notes: dict[str, Any] = field(default_factory=dict)

    def column(self, field_name: str) -> str:
        """反引号包裹列名（已包裹的不重复包裹）。"""
        return field_name if field_name.startswith("`") else f"`{field_name}`"

    def qualified_table(self) -> str:
        """表名按段加反引号（``db.tbl`` → ``` `db`.`tbl` ```）。"""
        return ".".join(f"`{part}`" for part in str(self.target_table or "").split("."))


@dataclass(frozen=True)
class FlowChecker:
    """流程型校验器的描述。

    ``run`` 是协程：``(ctx) -> list[CorrectnessViolation]``。
    抛异常会被编排层捕获并记为「未完成」，**不会**让整轮校验崩掉。
    """

    name: str
    run: Callable[[FlowContext], Awaitable[list[CorrectnessViolation]]]
    description: str = ""


# ============================================================================
# 模型调用
# ============================================================================


def resolve_llm(runtime: Any) -> Any:
    """取一个可用的模型实例；拿不到返回 ``None``（调用方据此降级）。

    优先用 ``runtime.llm("planner")`` —— 它按 env 的模型槽位配置解析，
    拿不到时退回全局默认模型。
    """
    if runtime is not None:
        getter = getattr(runtime, "llm", None)
        if callable(getter):
            try:
                model = getter("planner")
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[correctness] runtime.llm('planner') unavailable: {exc}")
            else:
                if model is not None:
                    return model
    try:
        from dataagent.core.managers.llm_manager import llm_manager

        return llm_manager.get_default_llm()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[correctness] default llm unavailable: {exc}")
        return None


async def invoke_llm_json(
    llm: Any,
    *,
    system: str,
    user: str,
    llm_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """调用模型并解析出一个 JSON 对象；解析失败返回 ``None``。

    **绝不抛异常** —— 模型不可用、网关报错、返回的不是 JSON，都归为
    「未完成」，由调用方降级处理。
    """
    if llm is None:
        return None
    try:
        from dataagent.core.managers.llm_manager.stream_utils import collect_llm_stream

        content = await collect_llm_stream(
            llm,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            **(llm_kwargs or {}),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[correctness] llm invocation failed: {exc!r}")
        return None
    if not content or not content.strip():
        return None
    try:
        from dataagent.utils.parsing_utils import extract_json_block

        parsed = extract_json_block(content)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[correctness] llm response is not valid JSON: {exc!r}")
        return None
    if isinstance(parsed, list):
        parsed = {"items": parsed}
    if not isinstance(parsed, dict):
        return None
    return parsed


# ============================================================================
# 只读 SQL 白名单
# ============================================================================

#: 明确禁止的写操作 / DDL 关键字（大小写不敏感）。
_FORBIDDEN_SQL_TOKENS = (
    "insert",
    "update",
    "delete",
    "drop",
    "truncate",
    "alter",
    "create",
    "replace",
    "merge",
    "grant",
    "revoke",
    "upsert",
    "load",
    "msck",
    "set",
    "call",
)

#: 只读语句必须以这些关键字开头。
_ALLOWED_SQL_PREFIXES = ("select", "with")


def is_read_only_sql(sql: str) -> bool:
    """判断一条 SQL 是否是**只读**查询。

    三道闸门：

    1. 必须以 ``SELECT`` / ``WITH`` 开头；
    2. 正文里不得出现写操作 / DDL 关键字；
    3. 只能是**一条**语句（按分号切分后非空片段数必须为 1）。

    用于校验「模型生成的回源查询」—— 那条 SQL 会被**真的执行**，
    所以只读性必须在执行前用程序卡死，不能靠提示词约束。
    """
    text = str(sql or "").strip()
    if not text:
        return False
    # 去掉行注释与块注释后再判关键字（注释里出现 "insert" 不该被拒）。
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)
    text = text.strip().rstrip(";").strip()
    if not text:
        return False
    lowered = text.lower()
    if not lowered.startswith(_ALLOWED_SQL_PREFIXES):
        return False
    # 多语句 —— 分号切分后必须只剩一条。
    statements = [part for part in text.split(";") if part.strip()]
    if len(statements) != 1:
        return False
    # 关键字扫描：按词边界匹配，避免 ``created_at`` 这类标识符误伤。
    return all(not re.search(rf"\b{token}\b", lowered) for token in _FORBIDDEN_SQL_TOKENS)


# ============================================================================
# 数值比对
# ============================================================================


def _to_float(raw: Any) -> float | None:
    """尽力把取值转成 float；转不了返回 ``None``。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def values_match(
    expected: Any,
    actual: Any,
    *,
    relative_tolerance: float = 1e-6,
    absolute_tolerance: float = 1e-9,
) -> bool:
    """比对「特征表取值」与「源表取值」是否一致。

    - 两侧都是 NULL → 一致；
    - 一侧 NULL 一侧非 NULL → 不一致；
    - 两侧都能转成数字 → 按 ``max(绝对容差, 相对容差 * |源表值|)`` 判等；
    - 其余按字符串精确比较（忽略首尾空白）。
    """
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    exp_num = _to_float(expected)
    act_num = _to_float(actual)
    if exp_num is not None and act_num is not None:
        tolerance = max(absolute_tolerance, relative_tolerance * abs(act_num))
        return abs(exp_num - act_num) <= tolerance
    return str(expected).strip() == str(actual).strip()


#: 常识判定里「四舍五入差异」的相对容差。
#:
#: 模型在 ``expected`` 里经常给四舍五入后的值（``0.3387``），而
#: ``observed_value`` 是原始精度（``0.338683``），差 ``1.7e-5`` —— 相对差
#: ``5e-5`` 远超回源比对的 ``1e-6``，但它不是违规，只是显示精度不同。
#:
#: 不能用回源比对那套容差：``1e-6`` 只适合「两侧都是原始精度」的场景，而
#: 常识判定的 ``expected`` 是模型手算的结果，天然带着显示精度。
_COMMON_SENSE_ROUNDING_RELATIVE_TOLERANCE = 1e-3

#: 常识判定里「四舍五入差异」的绝对容差。
#:
#: ``5e-4`` 覆盖「四舍五入到 3 位小数」的情况，留一个数量级的余量。
_COMMON_SENSE_ROUNDING_ABSOLUTE_TOLERANCE = 5e-4


def matches_with_rounding(
    expected: Any,
    actual: Any,
    *,
    relative_tolerance: float = _COMMON_SENSE_ROUNDING_RELATIVE_TOLERANCE,
    absolute_tolerance: float = _COMMON_SENSE_ROUNDING_ABSOLUTE_TOLERANCE,
) -> bool:
    """判断两个取值是否「在四舍五入精度内一致」。

    用于常识判定 —— 模型的 ``expected`` 常是手算四舍五入后的值
    （``0.3387``），而 ``observed_value`` 是原始精度（``0.338683``）。
    这种差异不是违规，不该触发自修正去「修」本来正确的 SQL。

    两侧都不是数值 → ``False``（交由调用方按字符串精确比较）；
    差值 ``<= max(绝对容差, 相对容差 * |实际值|)`` → 一致。

    与 :func:`values_match` 的区别：那边用 ``1e-6`` / ``1e-9``（两侧都是
    原始精度的场景）；这边用四舍五入感知的容差。
    """
    exp_num = _to_float(expected)
    act_num = _to_float(actual)
    if exp_num is None or act_num is None:
        return False
    tolerance = max(absolute_tolerance, relative_tolerance * abs(act_num))
    return abs(exp_num - act_num) <= tolerance


#: 从自由文本里抽数字的正则 —— 支持小数、科学计数法、可选正负号。
#:
#: 不会误伤「939」这种大数里的分毫号，也不会把 ``pt_d`` 里的下划线当分隔符。
_NUMERIC_TOKEN_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def extract_numeric_candidates(text: Any) -> tuple[float, ...]:
    """从自由文本里抽出所有数字（模型写的 ``reason`` / ``expected``）。

    常识判定的 ``expected`` 是自由文本，不是干净的数值字段 —— 只能从文本抽。
    抽出来是为了和实测值比对：文本里任一个数字与实测值在四舍五入精度内一致，
    说明模型给出的是同一个数的不同显示精度，不是违规。
    """
    if text is None:
        return ()
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return (float(text),)
    raw = str(text)
    if not raw.strip():
        return ()
    values: list[float] = []
    for token in _NUMERIC_TOKEN_RE.findall(raw):
        try:
            values.append(float(token))
        except ValueError:  # pragma: no cover - 正则保证可解析
            continue
    return tuple(values)


# ============================================================================
# 抽样
# ============================================================================


def build_sample_sql(
    table: str,
    columns: list[str],
    *,
    partition_filter: str = "",
    limit: int = 0,
) -> str:
    """生成抽样 SELECT（``SELECT <列清单> FROM <表> [WHERE 分区] LIMIT N``）。

    **必须显式列出列名** —— DataOps 平台禁止 ``SELECT *``。
    表名按段加反引号（``db.tbl`` → ``` `db`.`tbl` ```），避免被解析成单段标识符。

    **标识符与分区条件都过白名单**（见 :mod:`dataops_sql_identifiers`）——
    抽样列里可能混着**用户显式指定的字段**，不校验就是一条可注入的 SQL。
    校验不过时返回**空串**（调用方据此当「没抽到」处理），不抛异常。
    """
    if not columns:
        return ""
    try:
        cols_clause = ", ".join(quote_column(c) for c in columns)
        table_clause = quote_table(table)
        safe_filter = validate_partition_filter(partition_filter)
    except UnsafeIdentifierError as exc:
        logger.warning(f"[correctness_flow] sample sql rejected: {exc}")
        return ""
    where_clause = f" WHERE {safe_filter}" if safe_filter else ""
    limit_clause = f" LIMIT {int(limit)}" if limit and limit > 0 else ""
    return f"SELECT {cols_clause} FROM {table_clause}{where_clause}{limit_clause}"


async def sample_rows(
    ctx: FlowContext,
    columns: list[str],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], str]:
    """从目标表抽 ``limit`` 行完整数据。

    Returns:
        ``(rows, status)`` —— ``status`` 为 ``"completed"`` / ``"empty"`` /
        ``"no_executor"`` / ``"failed"``。
    """
    if ctx.execute_sql is None:
        return [], "no_executor"
    if not columns:
        return [], "no_columns"
    sql = build_sample_sql(
        ctx.target_table,
        columns,
        partition_filter=ctx.partition_filter,
        limit=limit,
    )
    if not sql:
        return [], "no_columns"
    try:
        status, rows = await ctx.execute_sql(sql)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[correctness] sample query failed: {exc!r}")
        return [], "failed"
    if status != "completed":
        return [], "failed"
    return list(rows or []), ("empty" if not rows else "completed")


def json_dumps(payload: Any) -> str:
    """稳定的 JSON 序列化（中文不转义）。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# ============================================================================
# 抽样数据 → 报告
# ============================================================================
#
# 报告里必须有抽样数据：校验结论（「抽样 20 行，发现 0 处违规」）没有内容
# 支撑 —— 用户无从判断模型到底看到了什么。口径错、采样偏、字段值异常，
# 全都表现成同一句「0 处违规」。把抽样到的原始行摊开，用户才能自己复核。
#
# 但要裁剪：一行可能有几十个字段、值还可能是几百字的枚举列表 —— 全量放进
# 报告会把消息撑爆。所以每个字段值截断到 ``_MAX_CELL_CHARS`` 字符，长列表
# 只留前若干项并注明总数，数值 / 布尔 / ``None`` 原样保留。

#: 单个字段值的最大展示长度 —— 超长就截断（完整值留在落盘的 JSON 里）。
_MAX_CELL_CHARS = 120

#: 长列表最多展示几项。
_MAX_LIST_ITEMS = 5


def record_sample_rows(
    rows: list[dict[str, Any]] | None,
    *,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """把抽样到的原始行整理成**可放进报告**的形状。

    **就地裁剪，不做行数限制** —— 行数由渲染层决定（不同校验项的抽样量
    不同）。这里只负责把**单元格**压短。

    Args:
        rows: 抽样到的原始行（``[{列名: 取值}, ...]``）。
        limit: 最多保留多少行；``0`` 表示不限制。

    Returns:
        ``[{"row_index": 1, "cells": {...}}, ...]``。
    """
    if not rows:
        return []
    recorded: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if limit and len(recorded) >= limit:
            break
        if not isinstance(row, dict):
            continue
        recorded.append(
            {
                "row_index": index,
                "cells": {str(key): _sample_cell(value) for key, value in row.items()},
            }
        )
    return recorded


def _sample_cell(value: Any) -> Any:
    """把一个字段值压成可展示的短文本。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(item) for item in value[:_MAX_LIST_ITEMS])
        if len(value) > _MAX_LIST_ITEMS:
            text += f", …（共 {len(value)} 项）"
        return _truncate(text)
    if isinstance(value, dict):
        text = ", ".join(f"{k}={v}" for k, v in list(value.items())[:_MAX_LIST_ITEMS])
        if len(value) > _MAX_LIST_ITEMS:
            text += f", …（共 {len(value)} 项）"
        return _truncate(text)
    return _truncate(str(value))


def _truncate(text: str) -> str:
    """截断过长的文本。"""
    if len(text) <= _MAX_CELL_CHARS:
        return text
    return text[:_MAX_CELL_CHARS] + "…"
