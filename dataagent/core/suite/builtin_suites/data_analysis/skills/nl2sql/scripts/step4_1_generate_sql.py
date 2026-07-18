"""
Step 4_1: 生成圈选 SQL
将 step3_5 决策树 / step3_6 评分卡 / step3_4 LightGBM 方案转为可部署 SQL。
衍生逻辑参考 step2_4_feature_derivation.md；表列经 schema_resolution 替换。

硬约束（原 Step12）：
- 不使用 CTE（WITH ... AS）
- 不使用 MODE() WITHIN GROUP、TRY_TO_NUMERIC、INTERVAL
- 无 LIMIT
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd

DATA_DIR = Path(os.environ.get("DATA_DIR", ".")).resolve()
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", DATA_DIR / "output")).resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SQL_DIR = Path(os.environ.get("SQL_DIR", OUTPUT_DIR / "sql")).resolve()
SQL_DIR.mkdir(parents=True, exist_ok=True)

FORBIDDEN_SQL_PATTERNS = [
    (re.compile(r"\bWITH\b", re.I), "CTE/WITH"),
    (re.compile(r"MODE\s*\(\s*\)\s*WITHIN\s+GROUP", re.I), "MODE() WITHIN GROUP"),
    (re.compile(r"\bTRY_TO_NUMERIC\b", re.I), "TRY_TO_NUMERIC"),
    (re.compile(r"\bINTERVAL\b", re.I), "INTERVAL"),
    (re.compile(r"\bLIMIT\b", re.I), "LIMIT"),
]


def load_schema_resolution() -> dict:
    """Load the logical-role to physical-column mapping from schema_resolution.json."""
    path = Path(
        os.environ.get("SCHEMA_RESOLUTION_PATH", str(OUTPUT_DIR / "schema_resolution.json"))
    ).resolve()
    if not path.is_file():
        raise SystemExit(
            f"schema_resolution not found: {path}. "
            "Run semantic_retrieve and write schema_resolution.json first."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    roles = data.get("roles", data)
    if not isinstance(roles, dict) or not roles:
        raise SystemExit(f"schema_resolution at {path} has empty roles map")
    return {str(k): str(v) for k, v in roles.items() if v is not None and str(v) != ""}


def apply_schema(sql: str, roles: dict) -> str:
    """Replace logical role placeholders in ``sql`` with physical column or table names."""
    out = sql
    for key in sorted(roles.keys(), key=len, reverse=True):
        out = out.replace(key, roles[key])
    leftover = sorted(set(re.findall(r"<[a-zA-Z0-9_]+>", out)))
    if leftover:
        raise SystemExit(
            "Unresolved logical-role placeholders: " + ", ".join(leftover)
        )
    return out


def assert_sql_constraints(sql: str, name: str) -> None:
    """Raise ``SystemExit`` if ``sql`` contains forbidden SQL constructs."""
    for pat, label in FORBIDDEN_SQL_PATTERNS:
        if pat.search(sql):
            raise SystemExit(f"{name}: forbidden construct {label} found in generated SQL")


def parse_tree_condition(condition_str: str) -> str:
    """Convert a decision-tree rule condition string into a SQL ``AND`` expression."""
    conditions = condition_str.split(" AND ")
    sql_conditions = []
    for cond in conditions:
        cond = cond.strip()
        if "<=" in cond:
            parts = cond.split("<=")
            sql_conditions.append(f"{parts[0].strip()} <= {float(parts[1].strip())}")
        elif ">" in cond:
            parts = cond.split(">")
            sql_conditions.append(f"{parts[0].strip()} > {float(parts[1].strip())}")
        elif "=" in cond:
            parts = cond.split("=")
            col = parts[0].strip()
            val = parts[1].strip().strip("'")
            if val == "__MISSING__" or val == "":
                sql_conditions.append(f"{col} IS NULL")
            elif val.isdigit() or (val.startswith("-") and val[1:].isdigit()):
                sql_conditions.append(f"{col} = {val}")
            else:
                sql_conditions.append(f"{col} = '{val}'")
    return " AND ".join(sql_conditions)


def generate_feature_derivation_sql() -> str:
    """
    部署侧特征衍生（无 CTE）：用子查询嵌套。
    完整衍生应以 step2_4_feature_derivation.md 为准，由 agent 按需扩展本骨架。
    """
    # city_tier + list length as illustrative non-CTE subqueries
    return "\n".join(
        [
            "-- ============================================",
            "-- Step 4_1: 特征衍生 SQL（参考 step2_4_feature_derivation.md）",
            "-- 约束: 无 CTE / 无 LIMIT / 无 MODE() WITHIN GROUP / 无 TRY_TO_NUMERIC / 无 INTERVAL",
            "-- ============================================",
            "",
            "SELECT",
            "  u.<user_id>,",
            "  CASE",
            "    WHEN REGEXP_REPLACE(COALESCE(u.<city>, ''), '市$', '') IN ('北京','上海','广州','深圳') THEN 1",
            "    WHEN REGEXP_REPLACE(COALESCE(u.<city>, ''), '市$', '') IN (",
            "      '成都','重庆','杭州','南京','武汉','西安','苏州','天津',",
            "      '长沙','郑州','合肥','青岛','大连','沈阳','宁波','昆明',",
            "      '济南','哈尔滨','福州','厦门','石家庄','南昌','南宁','贵阳','太原','长春'",
            "    ) THEN 2",
            "    WHEN u.<city> IS NOT NULL AND u.<city> != '' THEN 3",
            "    ELSE NULL",
            "  END AS city_tier,",
            "  CASE",
            "    WHEN u.<game_interest_list> IS NULL OR u.<game_interest_list> = '' THEN 0",
            "    ELSE LENGTH(u.<game_interest_list>) - LENGTH(REPLACE(u.<game_interest_list>, '#', '')) + 1",
            "  END AS game_interest_list_count,",
            "  CASE",
            "    WHEN u.<game_interest_list> IS NULL OR u.<game_interest_list> = '' THEN 1 ELSE 0",
            "  END AS game_interest_list_is_empty",
            "FROM <user_table> u",
            "",
        ]
    )


def generate_decision_tree_sql() -> str:
    """Build deployable SQL from the step3_5 decision-tree rule card."""
    rules_df = pd.read_csv(OUTPUT_DIR / "step3_5_rule_card.csv", encoding="utf-8-sig")
    rules_df = rules_df.drop_duplicates(subset=["rule_id"])

    case_exprs = []
    for _, row in rules_df.iterrows():
        sql_cond = parse_tree_condition(str(row["condition"]))
        case_exprs.append(f"CASE WHEN {sql_cond} THEN {row['score']} ELSE 0 END")

    score_expr = " + ".join(case_exprs) if case_exprs else "0"

    return "\n".join(
        [
            "-- ============================================",
            "-- step4_1: 决策树白盒 SQL（源自 step3_5_rule_card.csv）",
            f"-- 规则数: {len(rules_df)}",
            "-- 约束: 无 CTE / 无 LIMIT",
            "-- ============================================",
            "",
            "SELECT",
            "  t.<user_id>,",
            "  t.<label>,",
            "  t.final_score AS white_box_score",
            "FROM (",
            "  SELECT",
            "    u.<user_id>,",
            "    u.<label>,",
            f"    ({score_expr}) AS final_score",
            "  FROM <user_table> u",
            "  LEFT JOIN <behavior_agg> ba ON u.<user_id> = ba.<user_id>",
            "  LEFT JOIN <exposure_event> ee ON u.<user_id> = ee.<user_id>",
            ") t",
            "ORDER BY white_box_score DESC",
            "",
        ]
    )


def generate_scorecard_sql() -> str:
    """Build deployable SQL from the step3_6 scorecard rules."""
    rules_df = pd.read_csv(OUTPUT_DIR / "step3_6_score_rule.csv", encoding="utf-8-sig")

    case_exprs = []
    for _, row in rules_df.iterrows():
        feature = row["feature"]
        condition = str(row["condition"])
        weighted_score = row["weighted_score"]

        if "=" in condition:
            parts = condition.split("=")
            if "'__MISSING__'" in condition:
                case_expr = f"CASE WHEN {feature} IS NULL THEN {weighted_score} ELSE 0 END"
            elif "'" in condition:
                val = parts[1].strip().strip("'")
                case_expr = f"CASE WHEN {feature} = '{val}' THEN {weighted_score} ELSE 0 END"
            else:
                val = parts[1].strip()
                case_expr = f"CASE WHEN {feature} = {val} THEN {weighted_score} ELSE 0 END"
        elif "<=" in condition and ">" in condition:
            parts = condition.replace(" AND ", " ").split()
            gt_val = parts[parts.index(">") + 1]
            le_val = parts[parts.index("<=") + 1]
            case_expr = (
                f"CASE WHEN {feature} > {gt_val} AND {feature} <= {le_val} "
                f"THEN {weighted_score} ELSE 0 END"
            )
        elif "<=" in condition:
            val = condition.split("<=")[1].strip()
            case_expr = f"CASE WHEN {feature} <= {val} THEN {weighted_score} ELSE 0 END"
        elif ">" in condition:
            val = condition.split(">")[1].strip()
            case_expr = f"CASE WHEN {feature} > {val} THEN {weighted_score} ELSE 0 END"
        else:
            case_expr = f"0 /* 未解析: {condition} */"

        case_exprs.append(case_expr)

    score_expr = " + ".join(case_exprs) if case_exprs else "0"

    return "\n".join(
        [
            "-- ============================================",
            "-- step4_1: 评分卡 SQL（源自 step3_6_score_rule.csv）",
            f"-- 规则数: {len(rules_df)}",
            "-- 约束: 无 CTE / 无 LIMIT",
            "-- ============================================",
            "",
            "SELECT",
            "  t.<user_id>,",
            "  t.<label>,",
            "  t.final_score AS scorecard_score",
            "FROM (",
            "  SELECT",
            "    u.<user_id>,",
            "    u.<label>,",
            f"    ({score_expr}) AS final_score",
            "  FROM <user_table> u",
            "  LEFT JOIN <behavior_agg> ba ON u.<user_id> = ba.<user_id>",
            "  LEFT JOIN <exposure_event> ee ON u.<user_id> = ee.<user_id>",
            ") t",
            "ORDER BY scorecard_score DESC",
            "",
        ]
    )


def generate_lgb_score_approx_sql() -> str:
    """Build a LightGBM approximate scoring SQL skeleton from step3_4 artifacts."""
    return "\n".join(
        [
            "-- ============================================",
            "-- Step 4_1: LightGBM 近似评分 SQL 骨架（源自 step3_4）",
            "-- 按 step3_4_feature_importance / step3_4_model_report 填充加权式",
            "-- 约束: 无 CTE / 无 LIMIT",
            "-- ============================================",
            "",
            "SELECT",
            "  t.<user_id>,",
            "  t.<label>,",
            "  t.rough_score AS lgb_approx_score",
            "FROM (",
            "  SELECT",
            "    u.<user_id>,",
            "    u.<label>,",
            "    0 AS rough_score",
            "  FROM <user_table> u",
            ") t",
            "ORDER BY lgb_approx_score DESC",
            "",
        ]
    )


if __name__ == "__main__":
    print("加载 schema_resolution...")
    roles = load_schema_resolution()
    print(f"  roles: {len(roles)} keys")

    # optional: remind agent about derivation doc
    deriv = OUTPUT_DIR / "step2_4_feature_derivation.md"
    if deriv.is_file():
        print(f"  found derivation doc: {deriv}")
    else:
        print("  WARN: step2_4_feature_derivation.md not in OUTPUT_DIR; extend SQL from upstream artifact if needed")

    print("生成SQL文件...")
    outputs = [
        ("step4_1_feature_derivation.sql", generate_feature_derivation_sql()),
        ("step4_1_decision_tree.sql", generate_decision_tree_sql()),
        ("step4_1_scorecard.sql", generate_scorecard_sql()),
        ("step4_1_lgb_approx.sql", generate_lgb_score_approx_sql()),
    ]
    for name, raw in outputs:
        resolved = apply_schema(raw, roles)
        assert_sql_constraints(resolved, name)
        out = SQL_DIR / name
        out.write_text(resolved, encoding="utf-8")
        print(f"  生成: {out}")

    print("\nSQL生成完成!")
