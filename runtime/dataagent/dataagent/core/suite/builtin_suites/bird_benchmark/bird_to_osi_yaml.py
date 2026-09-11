#!/usr/bin/env python3
"""BIRD sqlite + dev_tables.json → 语义服务 OSI YAML 生成器.

复刻 step1a 的纯 sqlite 读取逻辑（不调 LLM / 不编向量），产出可直接
POST /v1/osi/import 的 OSI YAML：
  - datasets/fields   ← data_table / data_column（列描述来自 database_description CSV）
  - column_values      ← data_column_value（sqlite distinct 采样值，对应 step1a sample_values）
  - edges.*_join_*     ← table/column_join_relationship（来自 dev_tables.json foreign_keys）
  - sql_processes      ← sql_process（train_cache.json 中同 db_id 的金标 SQL，喂 sql-few-shots）

导入后语义服务自动 fill-vectors，本地 step1c/1f2/1g/1h 的向量编码全部由服务端承担。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import yaml

_VALUE_MODES = ("sample", "text_distinct", "all_distinct")
_DEFAULT_TEXT_DISTINCT_MAX_CARDINALITY = 1_000
_DEFAULT_MAX_VALUES_PER_COLUMN = 10_000
_DEFAULT_MAX_VALUES_PER_DB = 100_000
_DEFAULT_MAX_YAML_BYTES = 9_000_000

# ── step1a 等价读取逻辑（来自 nl2sql/preprocess/sources/bird_metadata.py）──────


def validate_db_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError("database IDs may contain only letters, digits and underscores")
    return value


def load_table_info(db_path: Path, table: str) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"PRAGMA table_info(`{table}`);").fetchall()
    return [
        {
            "cid": r[0],
            "name": r[1],
            "type": r[2],
            "notnull": r[3],
            "default": r[4],
            "pk": r[5],
        }
        for r in rows
    ]


def load_foreign_keys(db_path: Path, table: str) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute(f"PRAGMA foreign_key_list(`{table}`);").fetchall()
    return [
        {
            "source_table": table,
            "target_table": r[2],
            "source_column": r[3],
            "target_column": r[4],
        }
        for r in rows
    ]


def _eligible_value_query(table: str, column: str) -> str:
    ascii_whitespace = "char(9) || char(10) || char(11) || char(12) || char(13) || ' '"
    return (
        f"SELECT DISTINCT `{column}` FROM `{table}` "
        f"WHERE `{column}` IS NOT NULL "
        f"AND trim(CAST(`{column}` AS TEXT), {ascii_whitespace}) != '' "
        f"AND length(CAST(`{column}` AS TEXT)) <= 100"
    )


def count_distinct_values(db_path: Path, table: str, column: str) -> int:
    sql = f"SELECT COUNT(*) FROM ({_eligible_value_query(table, column)});"
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return int(conn.execute(sql).fetchone()[0])


def load_sample_values(db_path: Path, table: str, column: str, n: int | None = 3) -> list[str]:
    if n is not None and n < 0:
        raise ValueError("sample_n must be non-negative")
    limit = "" if n is None else f" LIMIT {int(n)}"
    sql = f"{_eligible_value_query(table, column)} ORDER BY CAST(`{column}` AS TEXT){limit};"
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute(sql).fetchall()
    return [str(r[0]) for r in rows if r and r[0] is not None]


def load_desc_map(bird_dir: Path, db_id: str, table: str) -> dict[str, dict[str, str]]:
    csv_path = bird_dir / db_id / "database_description" / f"{table}.csv"
    if not csv_path.exists():
        return {}
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with csv_path.open("r", encoding=enc, newline="") as f:
                reader = csv.DictReader(f, skipinitialspace=True)
                out: dict[str, dict[str, str]] = {}
                for row in reader:
                    orig = (row.get("original_column_name") or "").strip()
                    if not orig:
                        continue
                    out[orig] = {
                        "column_name": (row.get("column_name") or "").strip(),
                        "column_description": (row.get("column_description") or "").strip(),
                        "value_description": (row.get("value_description") or "").strip(),
                    }
                return out
        except UnicodeDecodeError:
            continue
    return {}


def build_desc_payload(column_name: str, desc_row: dict[str, str], llm_desc: str = "") -> dict[str, str]:
    """与 step1a build_column_description_payload 完全一致。"""
    expanded_name = (desc_row.get("column_name") or "").strip()
    column_description = (desc_row.get("column_description") or "").strip()
    value_description = (desc_row.get("value_description") or "").strip()
    parts: list[str] = []
    if expanded_name:
        parts.append(f"Expanded Column Name: {expanded_name}")
    if column_description:
        parts.append(f"Column Description: {column_description}")
    if value_description:
        parts.append(f"Value Description: {value_description}")
    desc = " | ".join(parts)
    desc_simple = " | ".join([x for x in [expanded_name, column_description] if x])
    # D12: LLM 生成的 column_description_short 优先于 CSV 回退（不降级为 CSV 描述）
    desc_short = llm_desc or expanded_name or column_description or column_name
    return {
        "desc_short": desc_short,
        "column_description_short": desc_short,
        "desc_simple": desc_simple,
        "desc": desc,
        "value_description": value_description,
    }


# ── 主流程 ──────────────────────────────────────────────────────────────────


def _norm_type(t: str) -> str:
    t = (t or "").upper()
    if "INT" in t:
        return "INTEGER"
    if "CHAR" in t or "TEXT" in t or "CLOB" in t:
        return "TEXT"
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return "REAL"
    if "NUM" in t or "DEC" in t:
        return "NUMERIC"
    if "DATE" in t:
        return "DATE"
    if "BOOL" in t:
        return "BOOLEAN"
    return t or "TEXT"


def build_yaml(
    db_id: str,
    bird_dir: Path,
    tables_json: Path,
    train_cache: Path | None,
    sample_n: int = 3,
    llm_desc_cache: Path | None = None,
    semantic_db_id: str | None = None,
    train_cache_mode: str = "matching",
    *,
    value_mode: str = "sample",
    text_distinct_max_cardinality: int = _DEFAULT_TEXT_DISTINCT_MAX_CARDINALITY,
    max_values_per_column: int = _DEFAULT_MAX_VALUES_PER_COLUMN,
    max_values_per_db: int = _DEFAULT_MAX_VALUES_PER_DB,
    max_yaml_bytes: int = _DEFAULT_MAX_YAML_BYTES,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_db_id(db_id)
    if value_mode not in _VALUE_MODES:
        raise ValueError(f"value_mode must be one of {_VALUE_MODES}, got {value_mode!r}")
    if sample_n < 0:
        raise ValueError("sample_n must be non-negative")
    if text_distinct_max_cardinality < 0:
        raise ValueError("text_distinct_max_cardinality must be non-negative")
    for name, limit in (
        ("max_values_per_column", max_values_per_column),
        ("max_values_per_db", max_values_per_db),
        ("max_yaml_bytes", max_yaml_bytes),
    ):
        if limit <= 0:
            raise ValueError(f"{name} must be positive")

    # Task 10.3: keep every BIRD database in an independent semantic-service
    # namespace. The raw db_id still selects local benchmark files/metadata.
    service_db_id = semantic_db_id or f"bird_{db_id}"
    dev_tables = json.loads(tables_json.read_text(encoding="utf-8"))
    entry = next((e for e in dev_tables if e.get("db_id") == db_id), None)
    if not entry:
        raise SystemExit(f"db_id={db_id} not found in {tables_json}")

    db_path = bird_dir / db_id / f"{db_id}.sqlite"
    if not db_path.exists():
        raise SystemExit(f"sqlite not found: {db_path}")

    # D12: 加载 LLM 列描述 cache（generate_llm_column_desc.py 产出）
    llm_desc_map: dict[str, str] = {}
    if llm_desc_cache and llm_desc_cache.exists():
        llm_desc_map = json.loads(llm_desc_cache.read_text(encoding="utf-8"))
        print(f"[llm-desc] loaded {len(llm_desc_map)} entries from {llm_desc_cache}")

    table_names_orig = entry.get("table_names_original") or []
    col_names_orig = entry.get("column_names_original") or []
    fk_pairs = entry.get("foreign_keys") or []

    # sqlite 实际表
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        actual = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence';"
            ).fetchall()
        ]
    actual_set = set(actual)
    tables = [t for t in (table_names_orig or actual) if t in actual_set]

    datasets: list[dict[str, Any]] = []
    column_values: list[dict[str, Any]] = []
    column_stats: list[dict[str, Any]] = []
    for tbl in tables:
        cols = load_table_info(db_path, tbl)
        desc_map = load_desc_map(bird_dir, db_id, tbl)
        fk_cols = {fk["source_column"] for fk in load_foreign_keys(db_path, tbl)}
        fields: list[dict[str, Any]] = []
        for c in cols:
            cname = c["name"]
            llm_desc = llm_desc_map.get(f"{tbl}.{cname}", "")
            if llm_desc_cache and not llm_desc:
                raise SystemExit(f"missing LLM column description for {tbl}.{cname} in {llm_desc_cache}")
            dp = build_desc_payload(cname, desc_map.get(cname, {}), llm_desc=llm_desc)
            fields.append(
                {
                    "name": cname,
                    "type": _norm_type(c["type"]),
                    "description": dp["desc_simple"] or cname,
                    "custom_extensions": {
                        "is_primary_key": bool(c["pk"]),
                        "is_foreign_key": cname in fk_cols,
                        "column_name_en": cname,
                        "column_name_desc": dp["desc_short"] or cname,
                        # semantic-service c758d40 importer reads llm_context and
                        # persists it as column_description(_short). The literal
                        # column_description_short extension is ignored.
                        "llm_context": dp["column_description_short"] or cname,
                        "column_description": dp["desc"] or dp["desc_simple"] or cname,
                        "db_name_en": service_db_id,
                        "table_name_en": tbl,
                        "qualified_name": f"{service_db_id}.{tbl}.{cname}",
                        "value_nullable": not bool(c["notnull"]),
                        "status": "Active",
                    },
                }
            )
            # 采样值 → data_column_value（喂 column-value-info / searchValues）
            value_type = _norm_type(c["type"])
            text_distinct_column = value_mode == "text_distinct" and value_type == "TEXT"
            needs_eligible_count = value_mode == "all_distinct" or text_distinct_column or stats is not None
            eligible_count = count_distinct_values(db_path, tbl, cname) if needs_eligible_count else None
            overflow_fallback = bool(
                text_distinct_column and eligible_count is not None and eligible_count > text_distinct_max_cardinality
            )
            use_all = value_mode == "all_distinct" or (text_distinct_column and not overflow_fallback)
            if value_mode == "all_distinct":
                selection_policy = "all_distinct"
            elif text_distinct_column and not overflow_fallback:
                selection_policy = "text_distinct"
            elif overflow_fallback:
                selection_policy = "text_distinct_sample_fallback"
            else:
                selection_policy = "sample"
            assert eligible_count is not None or not use_all
            if use_all and eligible_count > max_values_per_column:
                raise ValueError(
                    f"per-column value cap exceeded for {tbl}.{cname}: "
                    f"eligible={eligible_count}, cap={max_values_per_column}"
                )
            query_limit = None if use_all else min(sample_n, max_values_per_column + 1)
            values = load_sample_values(db_path, tbl, cname, query_limit)
            if len(values) > max_values_per_column:
                raise ValueError(
                    f"per-column value cap exceeded for {tbl}.{cname}: "
                    f"selected={len(values)}, cap={max_values_per_column}"
                )
            if len(column_values) + len(values) > max_values_per_db:
                raise ValueError(f"per-database value cap exceeded for {db_id}: selected>{max_values_per_db}")
            if stats is not None:
                assert eligible_count is not None
                column_stats.append(
                    {
                        "table": tbl,
                        "column": cname,
                        "value_type": value_type,
                        "eligible": eligible_count,
                        "selected": len(values),
                        "selection_policy": selection_policy,
                        "overflow_fallback": overflow_fallback,
                    }
                )
            for i, v in enumerate(values):
                column_values.append(
                    {
                        "id": f"cv_{service_db_id}_{tbl}_{cname}_{i}".replace(" ", "_"),
                        "value": v,
                        "description": "",
                        "physical_ref": {
                            "dataset": f"{service_db_id}.{tbl}",
                            "column": cname,
                        },
                        "value_type": value_type,
                        "status": "Active",
                    }
                )
        datasets.append(
            {
                "name": f"{service_db_id}.{tbl}",
                "source": f"{service_db_id}.{tbl}",
                "description": "",
                "custom_extensions": {
                    "entity_type": "PhysicalTable",
                    "layer": "ODS",
                    "schema_name": service_db_id,
                    "table_name_en": tbl,
                    "db_name_en": service_db_id,
                    "qualified_name": f"{service_db_id}.{tbl}",
                    "table_id": f"{service_db_id}.{tbl}",
                    "source_type": "sqlite",
                    "status": "Active",
                },
                "fields": fields,
            }
        )

    # JOIN 关系：dev_tables.json foreign_keys（列索引对）→ 表级 + 列级
    tbl_of = {i: (table_names_orig[ti], cn) for i, (ti, cn) in enumerate(col_names_orig) if ti >= 0}
    tjoins: list[dict[str, Any]] = []
    cjoins: list[dict[str, Any]] = []
    seen_tj = set()
    for a, b in fk_pairs:
        if a not in tbl_of or b not in tbl_of:
            continue
        t1, c1 = tbl_of[a]
        t2, c2 = tbl_of[b]
        if t1 not in actual_set or t2 not in actual_set:
            continue
        expr = f"{service_db_id}.{t1}.{c1} = {service_db_id}.{t2}.{c2}"
        tk = frozenset((t1, t2))
        if tk not in seen_tj:
            seen_tj.add(tk)
            tjoins.append(
                {
                    "source_entity": f"{service_db_id}.{t1}",
                    "target_entity": f"{service_db_id}.{t2}",
                    "join_type": "INNER JOIN",
                    "join_condition": expr,
                    "cardinality": "N:1",
                    "intent": f"{t1} join {t2}",
                }
            )
        cjoins.append(
            {
                "source_dimension": f"{service_db_id}.{t1}.{c1}",
                "target_dimension": f"{service_db_id}.{t2}.{c2}",
                "join_type": "EQUALS",
                "expression": expr,
                "intent": f"{c1} foreign key",
            }
        )

    # sql_processes feed the service's global sql-few-shots endpoint. BIRD
    # train/dev db_ids do not overlap, so a full run imports the global corpus
    # once (mode=all) rather than duplicating it in every dev namespace.
    sql_procs: list[dict[str, Any]] = []
    if train_cache and train_cache.exists():
        tc = json.loads(train_cache.read_text(encoding="utf-8"))
        for idx, item in enumerate(tc.get("train_items", [])):
            if train_cache_mode == "none" or (train_cache_mode == "matching" and item.get("db_id") != db_id):
                continue
            sql_procs.append(
                {
                    "id": f"sql_{service_db_id}_{idx}",
                    "name": f"BIRD {db_id} train {idx}",
                    "qualified_name": f"{service_db_id}.sql.train_{idx}",
                    "sql_id": f"{service_db_id}_train_{idx}",
                    "expression": item.get("SQL", ""),
                    "source_tables": [service_db_id],
                    "intent": "bird_few_shot",
                    "query": item.get("question", ""),
                    "status": "Active",
                }
            )

    model: dict[str, Any] = {
        "name": f"{service_db_id}_semantic_model",
        "description": f"BIRD benchmark database {db_id}",
        "datasets": datasets,
        "custom_extensions": [
            {
                "vendor_name": "LIGHTONTO",
                "data": {"graph": {"nodes": {}}},
            }
        ],
    }
    graph_nodes = model["custom_extensions"][0]["data"]["graph"]["nodes"]
    if column_values:
        graph_nodes["column_values"] = column_values
    if sql_procs:
        graph_nodes["sql_processes"] = sql_procs
    edges: dict[str, Any] = {}
    if tjoins:
        edges["table_join_relationship"] = tjoins
    if cjoins:
        edges["column_join_relationship"] = cjoins
    if edges:
        model["custom_extensions"][0]["data"]["graph"]["edges"] = edges

    document = {"version": "0.1.1", "semantic_model": [model]}
    yaml_bytes = len(yaml.dump(document, allow_unicode=True, sort_keys=False, width=4096).encode("utf-8"))
    if stats is not None:
        stats_payload = {
            "db_id": db_id,
            "semantic_db_id": service_db_id,
            "value_mode": value_mode,
            "sample_n": sample_n,
            "limits": {
                "max_values_per_column": max_values_per_column,
                "max_values_per_db": max_values_per_db,
                "max_yaml_bytes": max_yaml_bytes,
                "text_distinct_max_cardinality": text_distinct_max_cardinality,
            },
            "columns": column_stats,
            "totals": {
                "columns": len(column_stats),
                "eligible": sum(item["eligible"] for item in column_stats),
                "selected": len(column_values),
                "yaml_bytes": yaml_bytes,
            },
            "top_high_cardinality": sorted(
                column_stats,
                key=lambda item: (-item["eligible"], item["table"], item["column"]),
            )[:20],
        }
        stats.clear()
        stats.update(stats_payload)
    if yaml_bytes > max_yaml_bytes:
        raise ValueError(f"YAML byte cap exceeded for {db_id}: bytes={yaml_bytes}, cap={max_yaml_bytes}")
    return document


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-id", required=True, type=validate_db_id)
    ap.add_argument(
        "--semantic-db-id",
        default=None,
        help="semantic-service namespace (default: bird_<db-id>)",
    )
    ap.add_argument("--bird-dir", default="nl2sql/data/dev/dev_databases")
    ap.add_argument("--tables-json", default="nl2sql/data/dev/dev_tables.json")
    ap.add_argument("--train-cache", default="nl2sql/data/few_shot_data/train_cache.json")
    ap.add_argument(
        "--train-cache-mode",
        choices=("matching", "all", "none"),
        default="matching",
        help="Import matching examples, the full global corpus once, or none",
    )
    ap.add_argument("--sample-n", type=int, default=3)
    ap.add_argument("--value-mode", choices=_VALUE_MODES, default="sample")
    ap.add_argument(
        "--text-distinct-max-cardinality",
        type=int,
        default=_DEFAULT_TEXT_DISTINCT_MAX_CARDINALITY,
        help="Fully import TEXT distinct values only at or below this eligible cardinality",
    )
    ap.add_argument("--max-values-per-column", type=int, default=_DEFAULT_MAX_VALUES_PER_COLUMN)
    ap.add_argument("--max-values-per-db", type=int, default=_DEFAULT_MAX_VALUES_PER_DB)
    ap.add_argument("--max-yaml-bytes", type=int, default=_DEFAULT_MAX_YAML_BYTES)
    ap.add_argument("--stats-json", default=None, help="Write serializable value cardinality/size stats to this path")
    ap.add_argument("--stats-only", action="store_true", help="Compute stats without writing an OSI YAML file")
    ap.add_argument(
        "--llm-desc-cache",
        default=None,
        help="JSON cache from generate_llm_column_desc.py (D12: LLM column_description_short)",
    )
    ap.add_argument("--output", default=None)
    a = ap.parse_args()
    if not a.stats_only and not a.output:
        ap.error("--output is required unless --stats-only is set")
    bird_dir = Path(a.bird_dir).resolve()
    stats: dict[str, Any] = {}
    yaml_doc = build_yaml(
        a.db_id,
        bird_dir,
        Path(a.tables_json).resolve(),
        Path(a.train_cache).resolve() if a.train_cache else None,
        a.sample_n,
        Path(a.llm_desc_cache).resolve() if a.llm_desc_cache else None,
        a.semantic_db_id,
        a.train_cache_mode,
        value_mode=a.value_mode,
        text_distinct_max_cardinality=a.text_distinct_max_cardinality,
        max_values_per_column=a.max_values_per_column,
        max_values_per_db=a.max_values_per_db,
        max_yaml_bytes=a.max_yaml_bytes,
        stats=stats,
    )
    if a.stats_json:
        stats_path = Path(a.stats_json)
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif a.stats_only:
        print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    if not a.stats_only:
        out = Path(a.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            yaml.dump(yaml_doc, f, allow_unicode=True, sort_keys=False, width=4096)
    else:
        out = "<stats-only>"
    # 统计
    m = yaml_doc["semantic_model"][0]
    n_fields = sum(len(d.get("fields") or []) for d in m["datasets"])
    cv = len(m["custom_extensions"][0]["data"]["graph"]["nodes"].get("column_values") or [])
    sp = len(m["custom_extensions"][0]["data"]["graph"]["nodes"].get("sql_processes") or [])
    e = m["custom_extensions"][0]["data"]["graph"].get("edges", {})
    print(
        f"[ok] {out}  datasets={len(m['datasets'])} fields={n_fields} "
        f"column_values={cv} sql_processes={sp} "
        f"tjoins={len(e.get('table_join_relationship', []))} cjoins={len(e.get('column_join_relationship', []))}",
        file=sys.stderr if a.stats_only and not a.stats_json else sys.stdout,
    )


if __name__ == "__main__":
    main()
