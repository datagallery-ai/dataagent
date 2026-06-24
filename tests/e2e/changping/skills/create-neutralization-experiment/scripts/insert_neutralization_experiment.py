import argparse
import json
import sqlite3
from typing import Any, Dict, List


TABLE_NAME = "neutralization_experiments"

TABLE_EXPERIMENTS = "experiments"


def quote_ident(name: str) -> str:
    # SQLite identifier quoting uses double-quotes; escape inner quotes by doubling.
    return '"' + name.replace('"', '""') + '"'


def get_table_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    cur = conn.cursor()
    cur.execute(f'PRAGMA table_info({quote_ident(table_name)});')
    cols = []
    for row in cur.fetchall():
        # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
        cols.append(row[1])
    return cols


def parse_dilution_factors(raw: str) -> str:
    """
    Store as TEXT in SQLite (matches how CSV importer stored it).
    Accept either:
    - a JSON-like list string: "[30,150,750]"
    - a comma-separated list: "30,150,750"
    - a single value: "100"
    """
    s = (raw or "").strip()
    if not s:
        return None

    # Try JSON first.
    try:
        v = json.loads(s)
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        pass

    # Fallback: comma-separated numbers.
    parts = [p.strip() for p in s.split(",") if p.strip() != ""]
    if not parts:
        return None
    try:
        nums = [float(p) if "." in p else int(p) for p in parts]
        return json.dumps(nums, ensure_ascii=False)
    except Exception:
        # Keep original string if we can't parse into numbers.
        return s


def main() -> None:
    ap = argparse.ArgumentParser(description=f"Insert a row into {TABLE_NAME}.")
    # ap.add_argument("--db", required=True, help="Abosulte path to sqlite database")
    ap.add_argument("--id", type=int, default=None, help="Optional explicit experiment id")
    ap.add_argument("--cell_sample_id", type=int, required=True)
    ap.add_argument("--inhibitor_sample_id", type=int, required=True)
    ap.add_argument("--pseudovirus_sample_id", type=int, required=True)
    ap.add_argument("--inhibitor_concentration", default=1, help="Numeric concentration (will be stored as INTEGER/REAL)")
    ap.add_argument("--dilution_factors", default="5,5,5,5", help="JSON-like list string or comma-separated list")
    ap.add_argument("--result_id", type=int, default=None, help="Optional result_id")
    args = ap.parse_args()

    import os
    db_path = os.environ.get('USER_SQLITE_PATH')
    conn = sqlite3.connect(db_path)
    try:
        cols = get_table_columns(conn, TABLE_NAME)
        if not cols:
            raise SystemExit(f"Table not found or empty: {TABLE_NAME}")

        expected_cols = [
            "id",
            "cell_sample_id",
            "inhibitor_sample_id",
            "pseudovirus_sample_id",
            "inhibitor_concentration",
            "dilution_factors",
            "result_id",
        ]
        missing = [c for c in expected_cols if c not in cols]
        if missing:
            raise SystemExit(f"Missing expected columns in {TABLE_NAME}: {missing}. Existing: {cols}")

        cur = conn.cursor()
        cur.execute('PRAGMA foreign_keys = ON;')
        if args.id is None:
            cur.execute(f'SELECT COALESCE(MAX({quote_ident("id")}), 0) + 1 FROM {quote_ident(TABLE_EXPERIMENTS)};')
            args_id = cur.fetchone()[0]
        else:
            args_id = args.id

        # Cast inhibitor_concentration to int when possible; else float.
        conc_raw = str(args.inhibitor_concentration).strip()
        if conc_raw.lower() in ("true", "false"):
            inhibitor_concentration: Any = 1 if conc_raw.lower() == "true" else 0
        else:
            try:
                inhibitor_concentration = int(conc_raw)
            except Exception:
                inhibitor_concentration = float(conc_raw)

        dilution_factors = parse_dilution_factors(args.dilution_factors)
        result_id = args.result_id

        data: Dict[str, Any] = {
            "id": args_id,
            "cell_sample_id": args.cell_sample_id,
            "inhibitor_sample_id": args.inhibitor_sample_id,
            "pseudovirus_sample_id": args.pseudovirus_sample_id,
            "inhibitor_concentration": inhibitor_concentration,
            "dilution_factors": dilution_factors,
            "result_id": result_id,
        }

        cur.execute(f"insert into {quote_ident(TABLE_EXPERIMENTS)} (id, start_date, submitter, operator, status, type) values ({args_id}, date(), 900001, NULL, 'NEW', 'neutralization')")
        insert_cols = expected_cols
        placeholders = ", ".join(["?"] * len(insert_cols))
        insert_sql = (
            f"INSERT INTO {quote_ident(TABLE_NAME)} "
            f"({', '.join(quote_ident(c) for c in insert_cols)}) "
            f"VALUES ({placeholders});"
        )
        # print(insert_sql)
        # print(data)
        cur.execute(insert_sql, tuple(data[c] for c in insert_cols))
        conn.commit()

        print(f"Inserted experiment into {TABLE_NAME}: id={args_id}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

