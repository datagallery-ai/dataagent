import argparse
import json
import sqlite3
import sys
from typing import Any, Optional

def quote_ident(name: str) -> str:
    # SQLite identifier quoting uses double-quotes; escape inner quotes by doubling.
    return '"' + name.replace('"', '""') + '"'


def to_number(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if s == "":
        return None
    try:
        return float(s)
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Check whether a sample meets remaining volume and concentration requirements.")
    # ap.add_argument("--db", required=True, type=str, help="Abosulte path to sqlite database")
    ap.add_argument("--sample_id", type=int, required=True, help="样本ID")
    ap.add_argument("--min_remaining_volume", type=float, required=True, help="最小余量(>=)")
    ap.add_argument("--min_concentration", type=float, required=True, help="最小浓度(>=)")
    args = ap.parse_args()

    # Try to make stdout encoding consistent for Chinese fields.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    import os
    db_path = os.environ.get('USER_SQLITE_PATH')
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        sql = (
            f"SELECT a.id, 'antibody_sample', "
            f"a.volume, a.concentration, w.status "
            f"FROM antibody_samples a left outer join wet_samples w on a.id=w.id "
            f"WHERE a.id = ?;"
        )
        cur.execute(sql, (args.sample_id,))
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"Sample not found in antibody_samples: sample_id={args.sample_id}")

        (_, sample_type, volume_raw, conc_raw, status) = row
        volume = to_number(volume_raw)
        conc = to_number(conc_raw)

        remaining_ok = volume is not None and volume >= args.min_remaining_volume
        concentration_ok = conc is not None and conc >= args.min_concentration

        ok = remaining_ok and concentration_ok
        result = {
            "ok": ok,
            "sample_id": args.sample_id,
            "sample_type": sample_type,
            "status": status,
            "remaining_volume": volume,
            "min_remaining_volume": args.min_remaining_volume,
            "concentration": conc,
            "min_concentration": args.min_concentration,
            "remaining_ok": remaining_ok,
            "concentration_ok": concentration_ok,
        }
        print(json.dumps(result, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

