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
"""抽样回源比对（需求 1.4）。

流程（严格按需求原文的顺序）::

    INSERT 落地的表 ──► 抽 3 行（含主键取值与全部校验字段取值）
                          │
                          ▼
                   LLM 读这 3 行的实际含义 + 源表 schema + 原始 INSERT SQL
                          │
                          ▼
                   为每行生成 1 条即席 SELECT（回源表取数）
                          │
                          ▼
                   执行 3 条即席 SQL（并发）
                          │
                          ▼
                   逐行逐字段比对源表结果 vs 特征表结果

**为什么必须回源而不能只查特征表**：特征表里的数字本身就是待验证对象。
只查特征表只能验证「内部自洽」（重复、非负），验证不了「口径对不对」。
回源是唯一能发现「数字算错了但看起来合理」的手段。

**为什么由 LLM 生成 SQL 而不是模板拼接**：源表可能有不同的分区口径、不同的
JOIN 结构、需要先聚合再关联 —— 模板拼接覆盖不了。但生成结果**必须**通过
只读白名单校验（:func:`is_read_only_sql`），不合规的直接丢弃、记「无法验证」，
**绝不执行**。

**源表 0 行 ≠ 不匹配**：源表过滤条件比特征任务更严格时这很常见，
判成不匹配会产生大量误报 —— 误报会让自修正去「修」本来正确的 SQL。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from utils.dataops_contracts import (
    SEVERITY_ERROR,
    CorrectnessViolation,
)
from utils.dataops_correctness_flow import (
    FlowChecker,
    FlowContext,
    invoke_llm_json,
    is_read_only_sql,
    json_dumps,
    sample_rows,
    values_match,
)
from utils.dataops_table_schema import fetch_semantic_columns
from utils.dataops_validate_tool import (
    expand_ctes_static,
    has_cte,
)

logger = logging.getLogger(__name__)

# ============================================================================
# Prompt
# ============================================================================

_SYSTEM_PROMPT = """你是数据核对助手，为**抽样回源比对**生成只读的取数 SQL。

规则：
1. 只输出 JSON，不要输出任何解释文字。
2. 每次调用只为**一行**抽样数据生成 **1 条** SELECT。
3. SQL 只能读**源表**（可以 JOIN 多张源表），**绝对不能**写数据、不能改表、
   不能 INSERT/UPDATE/DELETE/CREATE/DROP，不能有多条语句。
4. 必须显式列出要取的列（禁止 SELECT *），并用 `AS <特征字段名>` 起别名 ——
   别名必须与特征表的列名**完全一致**，比对就是按别名对齐的。
5. 必须带分区过滤条件（形如 `pt_d = '20260801'`）—— 生产查询不带分区会被拒。
   分区日期要与特征表一致；源表分区列名不同时用源表自己的分区列。
6. 查询要能**复现特征任务的计算口径**（同样的过滤、同样的聚合、同样的时间窗），
   而不是简单取源表的原始明细列。
7. 无法从给定信息推出可靠口径时，输出 `{"sql": null, "reason": "..."}` ——
   **不要瞎猜**。查不准比不查更糟。
8. **绝对不能用 CTE（`WITH ... AS (...)`）** —— 生产 adHoc 引擎**不支持**
   `WITH AS`，会把 `FROM _src` 当成物理表名并直接报错
   （`table(s) agapdw._src do not exist`）。需要复用子查询时直接写
   **派生表**（`FROM (SELECT ...) AS x`）。

输出格式：
{"sql": "SELECT ...", "reason": "口径说明"}
"""

_USER_TEMPLATE = """目标表：`{table}`（分区：{partition}）

原始 INSERT SQL：
```sql
{insert_sql}
```

源表清单：{source_tables}
{source_schema}

本次要核对的抽样行（第 {row_index} 行 / 共 {row_count} 行）：
```json
{row}
```

特征表的字段清单：{fields}

请生成从源表取数、用于与上面这行比对的一条只读 SELECT（列别名用特征表列名）。
"""


def build_reconcile_prompt(
    ctx: FlowContext,
    row: dict[str, Any],
    *,
    row_index: int,
    row_count: int,
    source_schema: dict[str, Any],
) -> str:
    """构造回源取数 SQL 的提示词。"""
    schema_text = "（未取到源表 schema）"
    if source_schema:
        lines = []
        for table, columns in source_schema.items():
            if isinstance(columns, dict):
                cols = ", ".join(f"{name}:{ctype}" for name, ctype in columns.items())
            else:
                cols = ", ".join(str(c) for c in columns)
            lines.append(f"- {table}: {cols}")
        schema_text = "\n".join(lines)
    return _USER_TEMPLATE.format(
        table=ctx.target_table,
        partition=ctx.partition_filter or "（未指定）",
        insert_sql=ctx.sql,
        source_tables=", ".join(ctx.source_tables) or "（未解析到源表）",
        source_schema=schema_text,
        row_index=row_index,
        row_count=row_count,
        row=json_dumps(row),
        fields=", ".join(ctx.fields),
    )


# ============================================================================
# 源表 schema
# ============================================================================


async def fetch_source_schemas(
    ctx: FlowContext,
    tool_context: Any,
) -> dict[str, Any]:
    """取源表的列与类型（``{表名: {列名: 类型}}``）。

    取不到不算失败 —— 降级为「不带 schema 生成 SQL」，模型仍有机会推出口径。
    """
    if tool_context is None or not ctx.source_tables:
        return {}
    try:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[correctness] semantic columns unavailable: {exc!r}")
        return {}

    schemas: dict[str, Any] = {}
    for table in ctx.source_tables:
        try:
            fields, types = await fetch_semantic_columns(table, context=tool_context)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[correctness] source schema for {table} unavailable: {exc!r}")
            continue
        schemas[table] = dict(types) if types else dict.fromkeys(fields, "")
    return schemas


# ============================================================================
# Checker
# ============================================================================

#: 分区过滤的最小要求 —— 生成结果里必须出现分区列，否则生产 adHoc 会拒。
_PARTITION_TOKEN_RE = re.compile(r"\bpt_[a-z0-9_]+\s*=", re.IGNORECASE)


async def run_source_reconciliation(
    ctx: FlowContext,
    tool_context: Any = None,
) -> list[CorrectnessViolation]:
    """抽样回源比对的流程。

    Args:
        ctx: 流程上下文。
        tool_context: 工具执行上下文（查源表 schema 用）。缺省时取
            ``ctx.tool_context``。

    Returns:
        结构化违规列表。降级时返回空列表并写 ``ctx.notes``。
    """
    tool_context = tool_context if tool_context is not None else ctx.tool_context
    if not ctx.source_tables:
        ctx.notes["source_reconciliation__status"] = "skipped"
        ctx.notes["source_reconciliation__reason"] = "未从 INSERT 解析出源表"
        return []
    if not ctx.primary_key_columns:
        ctx.notes["source_reconciliation__status"] = "skipped"
        ctx.notes["source_reconciliation__reason"] = "未提供主键列"
        return []
    if ctx.execute_sql is None:
        ctx.notes["source_reconciliation__status"] = "unavailable"
        ctx.notes["source_reconciliation__reason"] = "没有可用的查询执行器"
        return []
    if ctx.llm is None:
        ctx.notes["source_reconciliation__status"] = "unavailable"
        ctx.notes["source_reconciliation__reason"] = "模型不可用"
        return []

    limit = max(1, ctx.reconcile_sample_size)
    sample_columns = _sample_columns(ctx)
    rows, status = await sample_rows(ctx, sample_columns, limit=limit)
    if status == "empty":
        ctx.notes["source_reconciliation__status"] = "skipped"
        ctx.notes["source_reconciliation__reason"] = "抽样结果为空"
        return []
    if status != "completed":
        ctx.notes["source_reconciliation__status"] = "failed" if status == "failed" else "skipped"
        ctx.notes["source_reconciliation__reason"] = f"抽样失败（{status}）"
        return []

    ctx.notes["source_reconciliation__sample_rows"] = len(rows)
    # 把抽样到的实际数据记进 notes —— 回源比对是「抽样 3 行回源」，用户要看到
    # 的是「拿哪 3 行去回源的」。
    #
    # 必须记的原因：``comparisons`` 只记录**比对成功**的行 —— 被丢弃的（SQL
    # 不合规 / 源表无数据）不在里面。用户看到「成功比对 1 行」却不知道另外 2 行
    # 长什么样，就无法判断「那 2 行为什么没比上」。抽样数据是唯一能同时覆盖
    # 「比上了的」和「没比上的」两批行的证据。
    #
    # 记原始行（不裁剪）—— notes 是**数据源**，裁剪是**渲染层**的活。
    ctx.notes["source_reconciliation__sample_data"] = rows
    source_schema = await fetch_source_schemas(ctx, tool_context)

    # --- 逐行生成即席 SQL（并发）----------------------------------------
    async def _generate(row: dict[str, Any], index: int) -> tuple[str | None, str]:
        payload = await invoke_llm_json(
            ctx.llm,
            system=_SYSTEM_PROMPT,
            user=build_reconcile_prompt(
                ctx,
                row,
                row_index=index + 1,
                row_count=len(rows),
                source_schema=source_schema,
            ),
            llm_kwargs=ctx.llm_kwargs,
        )
        if payload is None:
            return None, "模型返回无法解析"
        sql = payload.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            reason = str(payload.get("reason") or "模型未给出 SQL")
            return None, reason
        return sql.strip(), str(payload.get("reason") or "")

    generated = await asyncio.gather(
        *(_generate(row, index) for index, row in enumerate(rows)),
        return_exceptions=True,
    )

    statements: list[tuple[int, str, str]] = []
    unverifiable: list[dict[str, Any]] = []
    for index, outcome in enumerate(generated):
        if isinstance(outcome, BaseException):
            unverifiable.append({"row_index": index + 1, "reason": f"{outcome!r}"})
            continue
        sql, reason = outcome
        if not sql:
            unverifiable.append({"row_index": index + 1, "reason": reason or "模型未给出 SQL"})
            continue
        # **只读白名单** —— 生成结果会被真的执行，必须程序卡死。
        if not is_read_only_sql(sql):
            unverifiable.append(
                {"row_index": index + 1, "reason": "生成的 SQL 不是只读查询，已丢弃"}
            )
            continue
        # **不能带 CTE** —— 生产 adHoc 的表名解析阶段把 ``FROM _src`` 当成
        # 物理表名，直接报 ``table(s) agapdw._src do not exist``。只读白名单
        # 允许 ``WITH`` 开头（它只管「不改数据」），管不了「引擎不支持」——
        # 所以这里必须**单独**拦一道，并把带 CTE 的 SQL **静态摊平**后
        # 再执行（摊平失败就丢弃，绝不提交一条引擎读不懂的 SQL）。
        expanded_sql = expand_ctes_static(sql)
        if has_cte(expanded_sql):
            unverifiable.append(
                {"row_index": index + 1, "reason": "生成的 SQL 带 CTE 且无法静态展开，已丢弃"}
            )
            continue
        sql = expanded_sql
        if not _PARTITION_TOKEN_RE.search(sql):
            unverifiable.append(
                {"row_index": index + 1, "reason": "生成的 SQL 缺少分区过滤，已丢弃"}
            )
            continue
        # 行号随 SQL 一起带 —— 不能靠下标反推（被丢弃的行会让下标错位）。
        statements.append((index, sql, reason))

    if not statements:
        ctx.notes["source_reconciliation__status"] = "skipped"
        ctx.notes["source_reconciliation__reason"] = "没有可执行的回源查询"
        # **必须用双下划线** —— notes 的收集方（``dataops_quality_check``
        # 的 ``_run_flow_checks``）按 ``<checker_name>__<suffix>`` 切分，
        # 单下划线写法的 key 永远收集不到，「无法验证的行」就丢了。
        ctx.notes["source_reconciliation__unverifiable"] = unverifiable
        return []

    # --- 并发执行 ---------------------------------------------------------
    async def _run(statement: str) -> tuple[str, list[dict[str, Any]] | None]:
        return await ctx.execute_sql(statement)

    outcomes = await asyncio.gather(
        *(_run(sql) for _, sql, _ in statements),
        return_exceptions=True,
    )

    # **记录真正执行过的回源 SQL** —— 用户要能在报告里看到「到底查了源表的
    # 什么」。没有这条，用户只能看到一句「成功比对 3 行」，完全无法判断
    # 是不是真的去源表取了数（这正是假报告最难被发现的地方）。
    ctx.notes["source_reconciliation__executed_sql"] = [
        {"row_index": index + 1, "sql": sql, "reason": reason} for index, sql, reason in statements
    ]

    violations: list[CorrectnessViolation] = []
    matched = 0
    compared_fields = 0
    missing_fields: list[str] = []
    # **每行比对明细** —— 记录「这一行比了哪些字段、特征表取值 vs 源表取值」。
    # 用户要看到的正是这个：回源到底查出了什么数据、跟特征表差在哪。
    comparisons: list[dict[str, Any]] = []
    for (row_index, _sql, _reason), outcome in zip(statements, outcomes, strict=False):
        row = rows[row_index]
        if isinstance(outcome, BaseException):
            unverifiable.append({"row_index": row_index + 1, "reason": f"{outcome!r}"})
            continue
        status, source_rows = outcome
        if status != "completed":
            unverifiable.append(
                {"row_index": row_index + 1, "reason": f"回源查询未完成（{status}）"}
            )
            continue
        if not source_rows:
            # 源表 0 行 → **无法验证**，不是不匹配。
            unverifiable.append({"row_index": row_index + 1, "reason": "源表查询返回 0 行"})
            continue
        row_violations, row_compared, row_missing = _compare_row(
            ctx, row, source_rows[0], row_index + 1
        )
        if row_compared == 0:
            # **查询成功但一个字段都没比对上** —— 列名对不上（别名没按特征
            # 字段名起），拿到的源表数据跟特征表对不齐。这种情况**绝不能**算
            # 「比对通过」：把「没比」报成「比过了且一致」是整条校验里最危险
            # 的假阳性 —— 它会让口径完全错误的 SQL 顺利交付。
            unverifiable.append(
                {
                    "row_index": row_index + 1,
                    "reason": (
                        "回源查询未返回任何可对齐的字段列"
                        f"（期望列：{', '.join(ctx.fields)}；"
                        f"实际列：{', '.join(str(k) for k in source_rows[0])}）"
                    ),
                    "expected_columns": list(ctx.fields),
                    "actual_columns": [str(k) for k in source_rows[0]],
                }
            )
            continue
        matched += 1
        compared_fields += row_compared
        if row_missing:
            missing_fields.extend(row_missing)
        violations.extend(row_violations)
        # 记下这一行的比对明细（源表实际返回的值 vs 特征表的值）。
        comparisons.append(
            {
                "row_index": row_index + 1,
                "primary_key": {key: row.get(key) for key in ctx.primary_key_columns},
                "compared_fields": row_compared,
                "missing_fields": row_missing,
                "source_row": {str(key): source_rows[0][key] for key in source_rows[0]},
                "feature_row": {field_name: row.get(field_name) for field_name in ctx.fields},
            }
        )

    ctx.notes["source_reconciliation__status"] = "completed"
    ctx.notes["source_reconciliation__unverifiable"] = unverifiable
    ctx.notes["source_reconciliation__matched_rows"] = matched
    # **实际比对过的字段数** —— 与 ``matched_rows`` 分开记。``matched_rows``
    # 只说明「这一行的回源查询成功且列能对齐」，``compared_fields`` 才说明
    # 「真的拿几个字段值做了比对」。两者不一致时报告必须写清楚，否则
    # 「成功比对 3 行」会被读成「3 行 × 全部字段都核对过」。
    ctx.notes["source_reconciliation__compared_fields"] = compared_fields
    if comparisons:
        ctx.notes["source_reconciliation__comparisons"] = comparisons
    if missing_fields:
        # 去重但保留顺序。
        seen: set[str] = set()
        unique_missing = [f for f in missing_fields if not (f in seen or seen.add(f))]
        ctx.notes["source_reconciliation__missing_fields"] = unique_missing
    return violations


def _sample_columns(ctx: FlowContext) -> list[str]:
    """抽样时取哪些列 —— 主键列（必须有）+ 其余校验字段。"""
    columns = list(ctx.primary_key_columns)
    for name in ctx.fields:
        if name not in columns:
            columns.append(name)
    return columns


def _compare_row(
    ctx: FlowContext,
    feature_row: dict[str, Any],
    source_row: dict[str, Any],
    row_index: int,
) -> tuple[list[CorrectnessViolation], int, list[str]]:
    """逐字段比对「特征表取值」与「源表取值」。

    Returns:
        ``(violations, compared_fields, missing_fields)`` ——

        - ``violations``：取值不一致的字段；
        - ``compared_fields``：**实际比对过的字段数**。这是关键 —— 调用方要靠它
          区分「比过了且一致」与「一个字段都没比上」。后者（源表返回的列名
          与特征字段名对不上）必须算「无法验证」，不能算「通过」。
        - ``missing_fields``：源表结果里**没有**对应列的字段名（列名对不齐）。
    """
    violations: list[CorrectnessViolation] = []
    compared = 0
    missing: list[str] = []
    for field_name in ctx.fields:
        if field_name not in source_row:
            # 源表查询没返回这一列 —— **无法比对**，不是违规。
            # 这里只记下来，不静默吞掉：报告里要告诉用户「哪些字段没比上」。
            missing.append(field_name)
            continue
        compared += 1
        expected = feature_row.get(field_name)
        actual = source_row.get(field_name)
        if values_match(
            expected,
            actual,
            relative_tolerance=ctx.relative_tolerance,
            absolute_tolerance=ctx.absolute_tolerance,
        ):
            continue
        violations.append(
            CorrectnessViolation(
                rule="source_reconciliation",
                field=field_name,
                severity=SEVERITY_ERROR,
                message=(f"回源比对不一致：特征表取值 {expected!r}，源表取值 {actual!r}"),
                evidence={
                    "row_index": row_index,
                    "field": field_name,
                    "feature_value": expected,
                    "source_value": actual,
                    "primary_key": {key: feature_row.get(key) for key in ctx.primary_key_columns},
                },
                suggestion=(
                    f"复核 `{field_name}` 的口径：源表与特征表的过滤条件、"
                    "聚合方式或时间窗是否一致。"
                ),
            )
        )
    return violations, compared, missing


#: 流程型校验器。
SOURCE_RECONCILIATION_CHECKER = FlowChecker(
    name="source_reconciliation",
    run=run_source_reconciliation,
    description="抽样 3 行，按主键回源取数并逐字段比对",
)


__all__ = [
    "SOURCE_RECONCILIATION_CHECKER",
    "build_reconcile_prompt",
    "fetch_source_schemas",
    "run_source_reconciliation",
    "logger",
]
