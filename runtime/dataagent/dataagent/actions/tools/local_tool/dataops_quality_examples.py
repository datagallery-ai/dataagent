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
"""校验项「实际查出来的值」的采集与提炼。

校验指标本身都是**聚合值**（非空率 0.9500、主键重复率 0.0）—— 用户只看
得到数字，看不到「这个数字是算在哪几行上的」。本模块把**实际查到的取值**
收集起来，让报告里**每一项**校验都能配上自己的示例。

三类来源：

1. **统一口径抽样**（字段级校验：非空率 / 单一值比率 / 异常值占比 / 负值）
   —— 一次抽样拿到主键列 + 全部校验字段，逐字段从中提炼实际取值、取值
   频次、越界值、负值。
2. **重复主键查询**（主键重复率）—— 聚合查询只给比率，重复的具体是哪几组
   主键必须另查一条 ``GROUP BY ... HAVING COUNT(1) > 1``。
3. **各 checker 生成的 SQL** —— 报告里要能看出「这个结论是这条 SQL 算出来的」。
"""

from __future__ import annotations

from typing import Any

from dataagent.actions.tools.local_tool.dataops_sql_identifiers import (
    UnsafeIdentifierError,
    quote_column,
    quote_table,
    validate_partition_filter,
)
from dataagent.utils.log import logger

__all__ = [
    "CHECK_LABELS",
    "CHECK_ORDER",
    "build_check_examples",
    "build_duplicate_key_sql",
    "capture_duplicate_keys",
    "field_value_stats",
    "field_examples",
    "normalize_sample_rows",
]

#: 校验项的中文名（报告里用中文，不用内部英文 key）。
CHECK_LABELS: dict[str, str] = {
    "null_rate": "空值覆盖率",
    "single_value_ratio": "单一值比率",
    "outlier_ratio": "异常值占比",
    "negative_count": "计数型字段负值占比",
    "row_count": "数据行数",
    "pk_duplicate_rate": "主键重复率",
    "common_sense": "常识违规检测",
    "source_reconciliation": "抽样回源比对",
}

#: 校验项在报告里的**固定顺序** —— 顺序不变，用户不用每次重新扫。
CHECK_ORDER: tuple[str, ...] = (
    "null_rate",
    "single_value_ratio",
    "outlier_ratio",
    "negative_count",
    "row_count",
    "pk_duplicate_rate",
    "common_sense",
    "source_reconciliation",
)

#: 字段级校验项 —— 值示例按字段给出。
FIELD_SCOPED_CHECKS: frozenset[str] = frozenset(
    {"null_rate", "single_value_ratio", "outlier_ratio", "negative_count"}
)

#: 每个字段最多展示几个不同的取值。
MAX_EXAMPLE_VALUES = 5

#: 重复主键示例最多展示几组。
MAX_DUPLICATE_GROUPS = 5

#: 流程型校验（常识 / 回源）的抽样行最多内联几行。
MAX_FLOW_SAMPLE_ROWS = 5

#: 单个字段的示例文本上限（防止长字符串把表格撑爆）。
MAX_FIELD_EXAMPLE_CHARS = 120

#: 单个校验项的摘要文本上限。
MAX_SUMMARY_CHARS = 200


def normalize_sample_rows(sample_rows: Any) -> list[dict[str, Any]]:
    """把样本统一成 ``[{"row_index": int, "cells": {...}}]``。

    兼容两种形状：``{"row_index": n, "cells": {...}}``（统一口径抽样的产物）
    与 ``{"列名": 取值}``（流程型校验记进 notes 的原始行）。
    """
    rows: list[dict[str, Any]] = []
    if not isinstance(sample_rows, list):
        return rows
    for index, item in enumerate(sample_rows, start=1):
        if not isinstance(item, dict):
            continue
        cells = item.get("cells")
        if not isinstance(cells, dict):
            cells = {k: v for k, v in item.items() if k != "row_index"}
        rows.append({"row_index": item.get("row_index", index), "cells": dict(cells)})
    return rows


def _is_number(value: Any) -> bool:
    """是不是数值（布尔不算 —— ``True`` 不该被当成 1）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _value_text(value: Any) -> str:
    """把一个取值渲染成短文本。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    return text if text else ""


def field_value_stats(
    sample_rows: Any,
    fields: list[str] | tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """逐字段统计样本里的**实际取值**。

    每个字段给出：抽样里出现了多少行、有多少空值、出现过哪些不同的取值、
    最高频的取值与次数、越界取值（比率类 <0 或 >1）、负值。

    **统计口径是抽样行** —— 与聚合校验（基于同一批采样行）一致，所以示例值
    确实就是「这个指标算在哪些数据上」。
    """
    rows = normalize_sample_rows(sample_rows)
    stats: dict[str, dict[str, Any]] = {}
    for field in fields:
        values: list[Any] = []
        null_count = 0
        present = 0
        for row in rows:
            cells = row["cells"]
            if field not in cells:
                continue
            present += 1
            value = cells[field]
            if value is None:
                null_count += 1
                continue
            values.append(value)

        counts: dict[str, int] = {}
        for value in values:
            key = _value_text(value)
            counts[key] = counts.get(key, 0) + 1
        top = max(counts.items(), key=lambda kv: kv[1]) if counts else None

        stats[field] = {
            "total": present,
            "null_count": null_count,
            "values": values,
            "distinct": counts,
            "top": top,
            "outliers": [
                value for value in values if _is_number(value) and (float(value) < 0 or float(value) > 1)
            ],
            "negatives": [value for value in values if _is_number(value) and float(value) < 0],
        }
    return stats


def field_examples(
    sample_rows: Any,
    fields: list[str] | tuple[str, ...],
) -> dict[str, str]:
    """每个字段的「实际取值」摘要（给字段级指标表当示例列）。

    摘要形如 ``0，3，12（空 1/5 行）`` —— 取值直接来自抽样行，用户能一眼
    对上「非空率 0.9500」是怎么算出来的。
    """
    stats = field_value_stats(sample_rows, fields)
    result: dict[str, str] = {}
    for field, info in stats.items():
        if not info["total"]:
            result[field] = "（未抽到该字段）"
            continue
        distinct = list(info["distinct"].items())
        parts = [text for text, _ in distinct[:MAX_EXAMPLE_VALUES]]
        if len(distinct) > MAX_EXAMPLE_VALUES:
            parts.append("…")
        text = "，".join(part for part in parts if part != "") or "（全为空）"
        if info["null_count"]:
            text += f"（空 {info['null_count']}/{info['total']} 行）"
        result[field] = _truncate(text, MAX_FIELD_EXAMPLE_CHARS)
    return result


def _truncate(text: str, limit: int) -> str:
    """截断过长的文本（保留省略号）。"""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def build_duplicate_key_sql(
    table: str,
    primary_key_columns: tuple[str, ...] | list[str],
    *,
    partition_filter: str = "",
    limit: int = MAX_DUPLICATE_GROUPS,
) -> str:
    """生成「哪些主键重复了」的示例查询。

    形状::

        SELECT `udid`, COUNT(1) AS cnt
        FROM `db`.`tbl`
        WHERE <分区条件> AND `udid` IS NOT NULL
        GROUP BY `udid`
        HAVING COUNT(1) > 1
        LIMIT 5

    **为什么必须另查一条**：主键重复率那条聚合 SQL 只返回一个比率，重复的
    具体是哪几组主键它给不出来 —— 报告里写「重复率 0.02」用户无从下手。

    **为什么用派生表而不是 CTE**：生产 adHoc 的表名解析阶段会把 ``FROM _src``
    当成物理表名去解析（见 ``dataops_quality_check._sample_source``）。这里
    不需要派生表 —— 直接查物理表。

    **标识符必须过白名单**（见 :mod:`dataops_sql_identifiers`）：表名与主键列名
    会被拼进 SQL 文本，而它们可能来自**用户显式指定的字段**（``fields=``）——
    不校验就是一条可注入的 SQL。分区过滤条件同理。

    返回空串表示「拼不出来」—— 表名/列名不合法时**不抛异常**，让调用方把它
    当成「没查成」处理（报告里注明原因），而不是让整轮校验崩掉。
    """
    if not table or not primary_key_columns:
        return ""

    # --- 标识符与过滤条件过白名单 -----------------------------------------
    try:
        table_clause = quote_table(table)
        cols = ", ".join(quote_column(name) for name in primary_key_columns)
        not_null = " AND ".join(
            f"{quote_column(name)} IS NOT NULL" for name in primary_key_columns
        )
        safe_filter = validate_partition_filter(partition_filter)
    except UnsafeIdentifierError as exc:
        logger.warning(f"[quality_check] duplicate key sql rejected: {exc}")
        return ""

    conditions = [part for part in (safe_filter.strip(), not_null) if part]
    where_sql = f"\nWHERE {' AND '.join(conditions)}" if conditions else ""
    limit_clause = f"\nLIMIT {int(limit)}" if limit and limit > 0 else ""
    return (
        f"SELECT {cols}, COUNT(1) AS cnt\n"
        f"FROM {table_clause}{where_sql}\n"
        f"GROUP BY {cols}\n"
        f"HAVING COUNT(1) > 1{limit_clause}"
    )


async def capture_duplicate_keys(
    *,
    execute: Any,
    table: str,
    primary_key_columns: tuple[str, ...] | list[str],
    partition_filter: str = "",
    limit: int = MAX_DUPLICATE_GROUPS,
) -> tuple[bool, list[dict[str, Any]]]:
    """查「重复的主键」明细。

    Returns:
        ``(是否成功, 明细行)`` —— 「没查到重复」与「没查到结果」必须能区分：
        前者是「查过了没问题」，后者是「没查成」，报告里不能混为一谈。
    """
    if execute is None or not table or not primary_key_columns:
        return False, []
    try:
        sql = build_duplicate_key_sql(
            table,
            primary_key_columns,
            partition_filter=partition_filter,
            limit=limit,
        )
        if not sql:
            return False, []
        status, rows = await execute(sql)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[quality_check] duplicate key query failed: {exc!r}")
        return False, []
    if status != "completed":
        return False, []
    if not rows:
        return True, []
    return True, [row for row in rows if isinstance(row, dict)]


def _null_rate_field_text(info: dict[str, Any]) -> str:
    """非空率这一项的字段级示例。"""
    if not info["total"]:
        return "（未抽到该字段）"
    distinct = list(info["distinct"].items())
    parts = [text for text, _ in distinct[:MAX_EXAMPLE_VALUES]]
    if len(distinct) > MAX_EXAMPLE_VALUES:
        parts.append("…")
    text = "，".join(part for part in parts if part != "") or "（全为空）"
    if info["null_count"]:
        text += f"（空 {info['null_count']}/{info['total']} 行）"
    return _truncate(text, MAX_FIELD_EXAMPLE_CHARS)


def _single_value_field_text(info: dict[str, Any]) -> str:
    """单一值比率这一项的字段级示例（最高频取值 + 次数）。"""
    if not info["total"]:
        return "（未抽到该字段）"
    if not info["top"]:
        return "（全部为空）"
    value, count = info["top"]
    text = f"最高频取值 {value}（{count}/{info['total']} 行）"
    if info["null_count"]:
        text += f"；另有空值 {info['null_count']} 行"
    return _truncate(text, MAX_FIELD_EXAMPLE_CHARS)


def _outlier_field_text(info: dict[str, Any]) -> str:
    """异常值占比这一项的字段级示例（越界取值）。"""
    if not info["total"]:
        return "（未抽到该字段）"
    outliers = info["outliers"]
    if not outliers:
        return "（抽样内无越界取值）"
    texts: list[str] = []
    for value in outliers[:MAX_EXAMPLE_VALUES]:
        texts.append(_value_text(value))
    if len(outliers) > MAX_EXAMPLE_VALUES:
        texts.append("…")
    return _truncate(f"越界取值：{'，'.join(texts)}", MAX_FIELD_EXAMPLE_CHARS)


def _negative_field_text(info: dict[str, Any]) -> str:
    """负值占比这一项的字段级示例（负值取值）。"""
    if not info["total"]:
        return "（未抽到该字段）"
    negatives = info["negatives"]
    if not negatives:
        return "（抽样内无负值）"
    texts = [_value_text(value) for value in negatives[:MAX_EXAMPLE_VALUES]]
    if len(negatives) > MAX_EXAMPLE_VALUES:
        texts.append("…")
    return _truncate(f"负值：{'，'.join(texts)}", MAX_FIELD_EXAMPLE_CHARS)


#: 字段级校验项 → 该项的字段示例文本生成器。
_FIELD_TEXT_BUILDERS = {
    "null_rate": _null_rate_field_text,
    "single_value_ratio": _single_value_field_text,
    "outlier_ratio": _outlier_field_text,
    "negative_count": _negative_field_text,
}


def _join_field_texts(field_texts: dict[str, str], limit: int = 3) -> str:
    """把多个字段的示例拼成一行摘要（字段太多时只留前几个）。"""
    if not field_texts:
        return ""
    items = list(field_texts.items())
    parts = [f"`{name}`={text}" for name, text in items[:limit]]
    if len(items) > limit:
        parts.append(f"（另有 {len(items) - limit} 个字段）")
    return _truncate("；".join(parts), MAX_SUMMARY_CHARS)


def _duplicate_summary(
    duplicate_rows: list[dict[str, Any]] | None,
    duplicate_ok: bool,
    primary_key_columns: tuple[str, ...] | list[str],
) -> tuple[str, str]:
    """主键重复率的摘要与示例组。"""
    pk_label = " + ".join(primary_key_columns) if primary_key_columns else "主键"
    if not duplicate_ok:
        return "（未取到重复主键明细）", ""
    if not duplicate_rows:
        return f"（未发现重复的{pk_label}）", ""
    parts: list[str] = []
    for row in duplicate_rows[:MAX_DUPLICATE_GROUPS]:
        key = " + ".join(
            _value_text(row.get(name)) for name in primary_key_columns if name in row
        )
        cnt = row.get("cnt")
        parts.append(f"{key}（{_value_text(cnt)} 行）")
    extra = len(duplicate_rows) - MAX_DUPLICATE_GROUPS
    if extra > 0:
        parts.append(f"（另有 {extra} 组未展示）")
    summary = f"重复的{pk_label}：" + "、".join(parts)
    return _truncate(summary, MAX_SUMMARY_CHARS), "\n".join(parts)


def _flow_sample_rows(note: Any) -> list[dict[str, Any]]:
    """取流程型校验抽到的原始行。

    流程型校验（常识 / 回源比对）**自己抽样**（20 行 / 3 行），口径与统一
    抽样不同 —— 数据记在 ``notes["<name>"]["sample_data"]``。
    """
    if not isinstance(note, dict):
        return []
    return normalize_sample_rows(note.get("sample_data") or [])


def _flow_sample_text(rows: list[dict[str, Any]], limit: int = 3) -> str:
    """把抽样行压成一行摘要（``字段=取值`` 形式）。"""
    if not rows:
        return ""
    parts: list[str] = []
    for row in rows[:limit]:
        cells = row["cells"]
        items = [
            f"{key}={_value_text(value)}"
            for key, value in list(cells.items())[:4]
        ]
        parts.append("，".join(items))
    if len(rows) > limit:
        parts.append(f"（另有 {len(rows) - limit} 行未展示）")
    return _truncate("；".join(parts), MAX_SUMMARY_CHARS)


def _reconcile_comparison_text(note: Any) -> tuple[str, str]:
    """回源比对的实际取值示例。

    **回源比对的核心证据是「两侧的取值」** —— 特征表查到的值 vs 回源查到的
    值。只给「成功比对 3 行」是不够的：用户要看到**回源到底查出了什么**、
    跟特征表差在哪。

    Returns:
        ``(摘要, 逐行明细)``。
    """
    if not isinstance(note, dict):
        return "", ""
    comparisons = note.get("comparisons")
    if not isinstance(comparisons, list) or not comparisons:
        return "", ""

    lines: list[str] = []
    for index, item in enumerate(comparisons[:MAX_DUPLICATE_GROUPS], start=1):
        if not isinstance(item, dict):
            continue
        row_index = item.get("row_index", index)
        feature = item.get("feature_row")
        source = item.get("source_row")
        if not isinstance(feature, dict):
            feature = {}
        if not isinstance(source, dict):
            source = {}
        fields = sorted(set(feature) & set(source))
        diffs: list[str] = []
        for field in fields:
            left = feature.get(field)
            right = source.get(field)
            mark = "" if _values_equal(left, right) else " ⚠️不一致"
            diffs.append(f"{field}：特征表={_value_text(left)} / 回源={_value_text(right)}{mark}")
        if not diffs:
            continue
        lines.append(f"第 {row_index} 行：" + "；".join(diffs))

    if not lines:
        return "", ""
    summary = f"比对 {len(comparisons)} 行，逐行取值见下"
    return _truncate(summary, MAX_SUMMARY_CHARS), "\n".join(lines)


def _values_equal(left: Any, right: Any) -> bool:
    """两个取值是否一致（供回源比对标注「一致 / 不一致」）。"""
    try:
        from dataagent.actions.tools.local_tool.dataops_correctness_flow import values_match

        return values_match(left, right)
    except Exception:  # noqa: BLE001
        return str(left).strip() == str(right).strip()


def build_check_examples(
    *,
    check_sqls: dict[str, str] | None = None,
    fields: list[str] | tuple[str, ...] = (),
    check_fields: dict[str, list[str]] | None = None,
    sample_rows: Any = None,
    table_metrics: dict[str, Any] | None = None,
    duplicate_rows: list[dict[str, Any]] | None = None,
    duplicate_ok: bool = False,
    primary_key_columns: tuple[str, ...] | list[str] = (),
    flow_notes: dict[str, Any] | None = None,
    check_names: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """组装「每一项校验的实际查出来的值」。

    Args:
        check_fields: ``{校验项名: 该项实际适用的字段}``。**必须传** ——
            异常值占比只对比率类字段、负值校验只对计数型字段；不按适用性
            过滤会把「计数字段 3 > 1」当成越界取值报给用户。

    Returns:
        ``{校验项名: {"label", "summary", "sql", "fields", "groups"}}``。

        - ``summary`` —— 一行紧凑摘要（报告的「校验项执行情况」表里用）；
        - ``sql`` —— 该项实际提交的 SQL（让用户能追溯结论是怎么算出来的）；
        - ``fields`` —— 字段级校验项的逐字段取值示例；
        - ``groups`` —— 主键重复率的重复主键示例。
    """
    sqls = dict(check_sqls or {})
    metrics = dict(table_metrics or {})
    notes = dict(flow_notes or {})
    applicable = dict(check_fields or {})
    normalized = normalize_sample_rows(sample_rows)
    rows_available = bool(normalized)
    stats = field_value_stats(normalized, list(fields))
    field_summary = field_examples(normalized, list(fields))

    names = list(check_names) if check_names else list(sqls)
    ordered: list[str] = [name for name in CHECK_ORDER if name in names]
    ordered.extend(sorted(name for name in names if name not in CHECK_ORDER))

    examples: dict[str, Any] = {}
    for name in ordered:
        label = CHECK_LABELS.get(name, name)
        sql = sqls.get(name, "")
        item: dict[str, Any] = {
            "label": label,
            "summary": "",
            "sql": sql,
            "fields": {},
            "groups": "",
            "rows": [],
            "sample_text": "",
        }

        if name in FIELD_SCOPED_CHECKS:
            builder = _FIELD_TEXT_BUILDERS.get(name)
            scope_fields = applicable.get(name)
            if scope_fields is None:
                scope_fields = list(stats)
            if not rows_available:
                # 整轮没抽到样本 —— 说清是「没抽到」，不是「没有取值」。
                item["summary"] = "（本轮未抽到样本数据，无取值示例）"
                examples[name] = item
                continue
            texts = {
                field: (builder(stats[field]) if builder else field_summary.get(field, ""))
                for field in scope_fields
                if field in stats
            }
            item["fields"] = texts
            item["summary"] = _join_field_texts(texts)
            if not texts:
                item["summary"] = "（本项不适用于任何字段，无取值示例）"
        elif name == "row_count":
            value = metrics.get("row_count")
            item["summary"] = (
                f"实测 {int(value)} 行" if value is not None else "（未完成，行数未知）"
            )
        elif name == "pk_duplicate_rate":
            summary, groups = _duplicate_summary(
                duplicate_rows,
                duplicate_ok,
                primary_key_columns,
            )
            item["summary"] = summary
            item["groups"] = groups
        elif name in ("common_sense", "source_reconciliation"):
            note = notes.get(name)
            sample_rows = _flow_sample_rows(note)
            count = 0
            if isinstance(note, dict):
                count = note.get("sample_rows") or 0
                try:
                    count = int(count)
                except (TypeError, ValueError):
                    count = 0
            if not count:
                count = len(sample_rows)
            item["summary"] = (
                f"抽样 {count} 行" if count else "（未抽到样本数据）"
            )
            if sample_rows:
                item["rows"] = sample_rows[:MAX_FLOW_SAMPLE_ROWS]
                item["sample_text"] = _flow_sample_text(sample_rows)
            if name == "source_reconciliation":
                # 回源比对的核心证据是**两侧的取值** —— 特征表的值 vs 回源的值。
                summary, details_text = _reconcile_comparison_text(note)
                if details_text:
                    item["summary"] = (
                        f"{item['summary']}；{summary}" if item["summary"] else summary
                    )
                    item["groups"] = details_text

        examples[name] = item
    return examples
