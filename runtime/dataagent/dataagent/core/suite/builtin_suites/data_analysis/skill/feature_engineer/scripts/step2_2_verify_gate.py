#!/usr/bin/env python3
"""Single-source gate for the expanded step2_2 aggregation SQL (replaces 2a~2l).

Every check reads from the same two artifacts the expander wrote -- the expanded SQL and
``step2_2_feature_plan.json`` -- plus ``references/aggregation_policy.json``. Nothing is
re-derived by hand, so a hand-edited SQL or a policy mismatch fails here instead of
shipping. Groups:

  * 2a structure      -- exactly one CREATE, one EXCEPT, no diagnostics/USING, no leftovers
  * 2b column names   -- zero CJK characters in code (term literals excluded)
  * 2c list expansion -- per field ``has(splitByChar`` count == planned term count
  * 2d list pairs     -- every list field has both ``_list_length`` and ``_is_empty``
  * 2e 1:N coverage   -- every 1:N table has a CTE; every kept column has an aggregate;
                         every dropped column carries a policy reason
  * 2f aggregation    -- each aggregate function matches the policy for that column type
  * 2g dimensions     -- every dimension table appears in the SQL (zero-row dims exempt)
  * 2h residual lists -- no list field is projected as a bare column
  * 2i join nesting   -- delegate to ``step2_2_check_join_nesting.py`` (single source)
  * 2j geo tier       -- ``{col}_tier`` present and the raw geo column is EXCEPTed
  * 2k String CAST    -- 1:N derived String columns converted
  * 2l user-key uniq  -- join-right ``alias.*`` projections EXCEPT ``<user_id>``

Writes ``step2_2_feature_gate.json`` and touches ``gate_data_verified_2_2.txt`` on success.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from step2_2_check_join_nesting import _strip_comments_and_literals, check_join_nesting  # noqa: E402
from step2_2_expand_sql import (  # noqa: E402
    _aggregation_for,
    _classification_buckets,
    _classify_type,
    _key_validation,
    _read_json,
)

CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
DIAGNOSTIC_RE = re.compile(r"\bstep2_2_test\w*\b", re.IGNORECASE)
BLOCK_RE = re.compile(r"/\*__.*?__\*/", re.DOTALL)
PARAM_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

DEFAULT_PLAN = Path("step2_2_feature_plan.json")
DEFAULT_SQL = Path("step2_2_feature_aggregation_expanded.sql")
DEFAULT_VALIDATION = Path("step2_2_validation_expanded.sql")
DEFAULT_POLICY = SCRIPTS_DIR.parent / "references" / "aggregation_policy.json"
DEFAULT_REPORT = Path("step2_2_feature_gate.json")
DEFAULT_MARKER = Path("gate_data_verified_2_2.txt")

GROUPS = (
    ("2a", "结构：单条 CREATE / EXCEPT / 无诊断表"),
    ("2b", "列名：代码区无中文字符"),
    ("2c", "列表二元展开：词项数与计划一致"),
    ("2d", "列表列：_list_length 与 _is_empty 成对"),
    ("2e", "1:N 覆盖：CTE / 聚合列 / 丢弃 reason 齐全"),
    ("2f", "聚合合规：聚合函数与 aggregation_policy 一致"),
    ("2g", "维度表：全部出现在 SQL 中"),
    ("2h", "无残留列表原列"),
    ("2i", "JOIN 嵌套：每层 1 个 JOIN"),
    ("2j", "地理列：{col}_tier 存在且原列被 EXCEPT"),
    ("2k", "String CAST：1:N 派生 String 列已转换"),
    ("2l", "用户键唯一：JOIN 右表 * 投影 EXCEPT(<user_id>)"),
)


def _non_comment_sql(sql: str) -> str:
    """SQL with comments and literals blanked, for structural scans."""
    return _strip_comments_and_literals(sql)


def check_structure(sql: str, code: str) -> list[str]:
    errors: list[str] = []
    creates = len(re.findall(r"CREATE\s+(?:OR\s+REPLACE\s+)?TABLE", code, re.IGNORECASE))
    if creates != 1:
        errors.append(f"必须恰好 1 条 CREATE 语句，实际 {creates}")
    statements = [part for part in code.split(";") if part.strip()]
    if len(statements) > 1:
        errors.append(f"必须恰好 1 条语句，实际 {len(statements)} 段以分号分隔的 SQL")
    if "REPLACE TABLE" in code.upper() and "CREATE OR REPLACE TABLE" not in code.upper():
        errors.append("CREATE 必须使用 CREATE OR REPLACE TABLE")
    if "EXCEPT (" not in code:
        errors.append("最外层 SELECT 缺少 EXCEPT(...) 子句")
    leftovers = BLOCK_RE.findall(code)
    if leftovers:
        errors.append(f"仍含未展开动态块：{', '.join(sorted(set(leftovers)))}")
    params = PARAM_RE.findall(code)
    if params:
        errors.append(f"仍含未替换占位符：{', '.join(sorted(set(params)))}")
    diagnostics = DIAGNOSTIC_RE.findall(code)
    if diagnostics:
        errors.append(f"出现诊断/中间表：{', '.join(sorted(set(diagnostics)))}")
    if re.search(r"\bUSING\s*\(", code, re.IGNORECASE):
        errors.append("禁止 USING(...)，必须写 ON <left>.<key> = <right>.<key>")
    return errors


def check_column_names(code: str) -> list[str]:
    hits: list[str] = []
    for line_number, line in enumerate(code.splitlines(), start=1):
        match = CJK_RE.search(line)
        if match:
            hits.append(f"第 {line_number} 行：{match.group(0)}")
    if hits:
        return ["SQL 代码区出现中文字符（列名/别名禁止中文）：" + "; ".join(hits[:10])]
    return []


def check_list_expansion(code: str, plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    list_fields: list[dict[str, Any]] = []
    for entry in plan.get("aggregations") or []:
        for field in entry.get("list_fields") or []:
            list_fields.append(
                {
                    "label": f"{entry['table']}.{field['column']}",
                    "spelling": f"{entry['table']}_{field['column']}",
                    "terms": field.get("terms") or [],
                }
            )
    for field in plan.get("one_to_one_list_fields") or []:
        list_fields.append(
            {
                "label": f"{field['table']}.{field['column']}",
                "spelling": field["wide_column"],
                "terms": field.get("terms") or [],
            }
        )
    if list_fields and "splitByChar" not in code:
        errors.append("存在列表字段，但 SQL 中没有任何 splitByChar 展开")
    for field in list_fields:
        expected = len(field["terms"])
        produced = len(re.findall(
            r"\bAS\s+" + re.escape(field["spelling"]) + r"_term_[A-Za-z0-9_]+\b", code
        ))
        if produced != expected:
            errors.append(
                f"{field['label']}：{field['spelling']}_term_* 产出 {produced} 列 "
                f"≠ 计划词项数 {expected}"
            )
        for entry in field["terms"]:
            alias = f"{field['spelling']}_term_{entry['suffix']}"
            if alias not in code:
                errors.append(f"{field['label']}：缺少词项列 {alias}（{entry['term']}）")
    return errors


def check_list_pairs(code: str, plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    pairs: list[tuple[str, str]] = []
    for field in plan.get("one_to_one_list_fields") or []:
        spelling = field["wide_column"]
        pairs.append((spelling, spelling))
    for entry in plan.get("aggregations") or []:
        for field in entry.get("list_fields") or []:
            spelling = f"{entry['table']}_{field['column']}"
            pairs.append((spelling, spelling))
    for table, spelling in pairs:
        if f"{spelling}_list_length" not in code:
            errors.append(f"{spelling}：缺少 {spelling}_list_length")
        if f"{spelling}_is_empty" not in code:
            errors.append(f"{spelling}：缺少 {spelling}_is_empty")
    return errors


def check_one_to_many_coverage(
    sql: str,
    code: str,
    plan: dict[str, Any],
    schema: dict[str, Any],
    policy: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    _one_to_one, one_to_many, _dims = _classification_buckets(schema)
    expected_tables = [str(item.get("table_name") or "").strip() for item in one_to_many]
    planned = {entry["table"]: entry for entry in plan.get("aggregations") or []}

    for table in expected_tables:
        if not table:
            continue
        if table not in planned:
            errors.append(f"1:N 表 {table} 没有对应的聚合计划/CTE")
            continue
        entry = planned[table]
        if f"{entry['cte']} AS " not in code:
            errors.append(f"{table}：SQL 中找不到 CTE {entry['cte']} 的引用")
        if f"count() AS {table}_row_count" not in code:
            errors.append(f"{table}：缺少 {table}_row_count = count()")

    for table, entry in planned.items():
        reason_keys = set((policy.get("drop_reasons") or {}).keys())
        list_names = {str(field.get("column")) for field in entry.get("list_fields") or []}
        for item in entry.get("dropped") or []:
            if item.get("column") in list_names:
                errors.append(
                    f"{table}.{item.get('column')}：是 delimiter 列表字段（已做二元展开），"
                    "不得再出现在 dropped 里（列表分支不进类型→聚合表）"
                )
            if item.get("reason") not in reason_keys:
                errors.append(
                    f"{table}.{item.get('column')}：丢弃 reason {item.get('reason')!r} "
                    "不在 aggregation_policy.drop_reasons 中"
                )
        for column in entry.get("columns") or []:
            if column.get("column") in list_names:
                errors.append(
                    f"{table}.{column.get('column')}：是 delimiter 列表字段，"
                    "不得出现在类型→聚合列清单里"
                )
            if f"{entry['alias']}.{column.get('column')}" not in code:
                errors.append(
                    f"{table}.{column.get('column')}：SQL 中未按别名 {entry['alias']} 聚合并保留列"
                )
    return errors


def check_aggregation_compliance(
    code: str, plan: dict[str, Any], policy: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    for entry in plan.get("aggregations") or []:
        alias = entry.get("alias")
        table = entry.get("table")
        if not alias:
            errors.append(f"{table}：计划缺少 CTE 别名，无法校验聚合")
            continue
        for column in entry.get("columns") or []:
            name = column.get("column")
            declared_kind = str(column.get("kind") or "")
            derived_kind = _classify_type(str(column.get("type") or ""), policy)
            if derived_kind != declared_kind:
                errors.append(
                    f"{table}.{name}：计划 kind={declared_kind} 与按类型判定的 "
                    f"{derived_kind} 不一致（类型 {column.get('type')}）"
                )
                continue
            rule = _aggregation_for(derived_kind, policy)
            if rule is None:
                errors.append(f"{table}.{name}：类型 {derived_kind} 在策略表里没有聚合规则")
                continue
            expected_agg = str(rule["aggregation"])
            declared_agg = str(column.get("aggregation") or "")
            if declared_agg != expected_agg:
                errors.append(
                    f"{table}.{name}：计划聚合 {declared_agg}() ≠ aggregation_policy 的 "
                    f"{expected_agg}()"
                )
                continue
            expression = f"{expected_agg}({alias}.{name})"
            if expression not in code:
                errors.append(
                    f"{table}.{name}：SQL 中缺少策略要求的聚合表达式 {expression}"
                )
    return errors


def _is_zero_row_table(schema: dict[str, Any], table: str) -> bool:
    """True when a dimension was skipped because it contributes zero rows."""
    validation = _key_validation(schema).get(table) or {}
    total = validation.get("total_rows")
    try:
        if total is not None and int(total) == 0:
            return True
    except (TypeError, ValueError):
        pass
    if str(validation.get("classification") or "").strip() == "zero_row_skipped":
        return True
    return False


def check_dimensions(code: str, plan: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    _one_to_one, _one_to_many, dims = _classification_buckets(schema)
    expected = [str(item.get("table_name") or "").strip() for item in dims]
    planned = list(plan.get("dimension_tables") or [])
    for table in expected:
        if not table or _is_zero_row_table(schema, table):
            continue
        if table not in code:
            errors.append(f"维度表 {table} 未出现在 SQL 中")
    for table in planned:
        table = str(table or "").strip()
        if not table or _is_zero_row_table(schema, table):
            continue
        if table not in code:
            errors.append(f"计划中的维度表 {table} 未出现在 SQL 中")
    return errors


def check_usid_uniqueness(code: str, plan: dict[str, Any]) -> list[str]:
    """Require EXCEPT(<user_id>) only for join-right stars that re-emit the user key.

    - Aggregation aliases always re-emit ``<user_id>`` → must ``EXCEPT (<user_id>)``.
    - Dimension aliases must EXCEPT only when the physical dim columns include
      ``<user_id>`` (geo-only dims may project ``dim.*`` / ``dim.* EXCEPT (geo...)``).
    - Other ``alias.*`` projections that appear in SQL are not forced.
    Only validates projections that actually appear as ``, alias.*``.
    """
    user_id = str(plan.get("user_id") or "usid").strip() or "usid"
    agg_aliases = {
        str(item.get("alias") or "").strip()
        for item in (plan.get("aggregations") or [])
        if str(item.get("alias") or "").strip()
    }
    dim_aliases_needing_except: set[str] = set()
    for item in plan.get("dimension_plans") or []:
        alias = str(item.get("alias") or "").strip()
        if not alias:
            continue
        columns = item.get("columns") or []
        if user_id in columns:
            dim_aliases_needing_except.add(alias)
    required = agg_aliases | dim_aliases_needing_except

    errors: list[str] = []
    for match in re.finditer(
        r",\s*([A-Za-z_][A-Za-z0-9_]*)\.\*(?:\s+EXCEPT\s*\(([^)]*)\))?",
        code,
    ):
        alias = match.group(1)
        if alias not in required:
            continue
        except_body = match.group(2)
        if except_body is None:
            errors.append(
                f"{alias}.* 投影缺少 EXCEPT ({user_id})（用户键会重复透传）"
            )
            continue
        cols = {
            token.strip().strip("`")
            for token in except_body.split(",")
            if token.strip()
        }
        if user_id not in cols:
            errors.append(
                f"{alias}.* EXCEPT 未包含 {user_id}"
                f"（现有：{', '.join(sorted(cols)) or '空'}）"
            )
    return errors


def check_residual_list_columns(code: str, plan: dict[str, Any]) -> list[str]:
    """A list column must never survive as a bare projected column.

    For 1:1 list fields the only legitimate home is inside an ``EXCEPT (...)`` list. For
    1:N list fields the CTE must not project ``AS <table>_<col>`` without one of the
    derived suffixes. Asserting the positive location (rather than scanning for stray
    uses) is what catches a removed EXCEPT.
    """
    errors: list[str] = []
    except_groups = re.findall(r"EXCEPT\s*\(([^)]*)\)", code)
    excepted = {
        token.strip().strip("`")
        for group in except_groups
        for token in group.split(",")
        if token.strip()
    }

    for field in plan.get("one_to_one_list_fields") or []:
        spelling = str(field["wide_column"])
        if spelling not in excepted:
            errors.append(
                f"列表原列 {spelling} 未出现在任何 EXCEPT(...) 中，会以原始列表形态进入宽表"
            )

    for entry in plan.get("aggregations") or []:
        for field in entry.get("list_fields") or []:
            spelling = f"{entry['table']}_{field['column']}"
            bare_alias = re.compile(
                r"\bAS\s+" + re.escape(spelling) + r"\b(?!_term_|_list_length|_is_empty)"
            )
            for line in code.splitlines():
                if bare_alias.search(line):
                    errors.append(
                        f"1:N 列表原列 {spelling} 被原样投影（应只输出 _term_/_list_length/"
                        f"_is_empty 派生列）：" + line.strip()[:120]
                    )
                    break
    return errors


def check_geo_columns(sql: str, code: str, plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    except_columns = set(plan.get("except_columns") or [])
    for item in plan.get("tier_columns") or []:
        output = str(item.get("output") or "")
        column = str(item.get("column") or "")
        if output and f"AS {output}" not in code:
            errors.append(f"地理列 {column}：SQL 中缺少 {output} 分级列")
        if column and column not in except_columns:
            errors.append(f"地理原列 {column} 未被 EXCEPT，会进入宽表（过拟合）")
    if "/*__" in code:
        errors.append("SQL 仍含未展开动态块")
    return errors


def check_string_casts(code: str, plan: dict[str, Any]) -> list[str]:
    """Block derived 1:N Strings that no cast rule converted.

    ``step2_2_validation.sql`` only allow-lists *physical* String columns, so a derived one
    left as ``String`` fails the warehouse high-cardinality gate. Catching it here avoids a
    full aggregation run before the failure surfaces.
    """
    errors: list[str] = []
    for column in plan.get("unconverted_required_casts") or []:
        errors.append(
            f"1:N 派生 String 列 {column} 未转换：宽表会残留 String 并触发高基数门禁。"
            "按 step2_2_run.json 的 after_collect 跑 sample → string-plan，再带 --string-plan 重跑 finalize"
        )
    for item in plan.get("cast_columns") or []:
        column = str(item.get("column") or "")
        if column and f"EXCEPT" in code and column not in code:
            errors.append(f"已声明的 CAST 列 {column} 未出现在 SQL 中")
    return errors


def check_validation_sql(sql: str, code: str, plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if "/*__" in code:
        errors.append("validation SQL 仍含未展开动态块 /*__...__*/")
    if "/*__GEO_COLUMN_MATCH__*/" in code:
        errors.append("validation SQL 未绑定 /*__GEO_COLUMN_MATCH__*/")
    if "/*__LIST_FIELD_NAMES__*/" in code:
        errors.append("validation SQL 未绑定 /*__LIST_FIELD_NAMES__*/")
    if "/*__ALLOWED_STRING_COLUMNS__*/" in code:
        errors.append("validation SQL 未绑定 /*__ALLOWED_STRING_COLUMNS__*/")
    # Regression: block payloads must never be substituted inside a ``--`` comment.
    # The geo matcher spans lines, so a naive replace corrupts the comment block and
    # injects bare SQL, while the placement checks above still pass.
    for line in sql.splitlines():
        if not line.lstrip().startswith("--"):
            continue
        if "match(name," in line or "position(name," in line:
            errors.append(
                "validation SQL 的注释行被注入了 SQL 载荷（动态块替换误伤注释）："
                + line.strip()[:120]
            )
    for field in plan.get("list_fields") or []:
        if f"'{field}'" not in code:
            errors.append(f"validation SQL 的列表字段清单缺少 {field}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="step2_2 聚合展开门禁（2a~2l）")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--sql", type=Path, default=DEFAULT_SQL)
    parser.add_argument("--validation", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument("--schema", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--report-json", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--marker", type=Path, default=DEFAULT_MARKER)
    args = parser.parse_args()

    if not args.plan.is_file():
        raise SystemExit(f"未找到聚合计划 {args.plan}；请先运行 step2_2_driver.py finalize")
    if not args.sql.is_file():
        raise SystemExit(f"未找到展开 SQL {args.sql}；请先运行 step2_2_driver.py finalize")

    plan = _read_json(args.plan)
    policy = _read_json(args.policy)
    sql = args.sql.read_text(encoding="utf-8")
    code = _non_comment_sql(sql)
    schema = _read_json(args.schema) if args.schema and args.schema.is_file() else {}

    validation_sql = ""
    validation_code = ""
    if args.validation.is_file():
        validation_sql = args.validation.read_text(encoding="utf-8")
        # Line comments dropped, string literals kept: the validation gate asserts on the
        # inlined field-name literals, which a literal-stripping scan would erase.
        validation_code = "\n".join(
            line
            for line in validation_sql.splitlines()
            if not line.lstrip().startswith("--")
        )

    checks: dict[str, list[str]] = {
        "2a": check_structure(sql, code),
        "2b": check_column_names(code),
        "2c": check_list_expansion(code, plan),
        "2d": check_list_pairs(code, plan),
        "2e": check_one_to_many_coverage(sql, code, plan, schema, policy)
        if schema
        else ["缺少 schema_resolution.json，无法校验 1:N 覆盖"],
        "2f": check_aggregation_compliance(code, plan, policy),
        "2g": check_dimensions(code, plan, schema) if schema else ["缺少 schema_resolution.json，无法校验维度表"],
        "2h": check_residual_list_columns(code, plan),
        "2i": check_join_nesting(sql),
        "2j": check_geo_columns(sql, code, plan),
        "2k": check_string_casts(code, plan),
        "2l": check_usid_uniqueness(code, plan),
    }
    if validation_sql:
        checks["2a"] = checks["2a"] + check_validation_sql(validation_sql, validation_code, plan)
    failed = {key: value for key, value in checks.items() if value}
    passed = not failed

    report = {
        "step": "step2_2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "checks": {
            key: {"passed": not value, "errors": value} for key, value in checks.items()
        },
        "sql": str(args.sql),
        "plan": str(args.plan),
    }
    args.report_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"已写出 {args.report_json}；{'全部通过' if passed else '存在失败项'}")
    for key, _label in GROUPS:
        status = "PASS" if not checks[key] else "FAIL"
        print(f"  {key} {status}")
        for error in checks[key]:
            print(f"    - {error}")

    if not passed:
        raise SystemExit(
            "step2_2 门禁失败："
            + " | ".join(f"{key}: {value[0]}" for key, value in failed.items())
        )

    args.marker.write_text(
        f"step2_2 gate verified at {report['generated_at']}，共 {len(GROUPS)} 组门禁全部通过\n",
        encoding="utf-8",
    )
    print(f"门禁通过，已写出 {args.marker}")


if __name__ == "__main__":
    main()
