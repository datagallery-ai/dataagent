# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================

import argparse
import contextlib
import json
import sqlite3
import sys

TABLE_EXPERIMENTS = "experiments"
TABLE_USERS = "users"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def main() -> None:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    ap = argparse.ArgumentParser(description="Assign a researcher (user) to an experiment via experiments.operator.")
    # ap.add_argument("--db", required=True, help="Abosulte path to sqlite database")
    ap.add_argument("--experiment_id", type=int, required=True)
    ap.add_argument("--researcher_user_id", type=int, required=True)
    args = ap.parse_args()

    import os

    db_path = os.environ.get("USER_SQLITE_PATH")
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT 1 FROM {quote_ident(TABLE_USERS)} WHERE {quote_ident('id')} = ?;",
            (args.researcher_user_id,),
        )
        if cur.fetchone() is None:
            raise SystemExit(f"User not found: researcher_user_id={args.researcher_user_id}")

        cur.execute(
            f"SELECT {quote_ident('id')} FROM {quote_ident(TABLE_EXPERIMENTS)} WHERE {quote_ident('id')} = ?;",
            (args.experiment_id,),
        )
        if cur.fetchone() is None:
            raise SystemExit(f"Experiment not found: experiment_id={args.experiment_id}")

        cur.execute(
            f"UPDATE {quote_ident(TABLE_EXPERIMENTS)} SET {quote_ident('operator')} = ? WHERE {quote_ident('id')} = ?;",
            (args.researcher_user_id, args.experiment_id),
        )
        if cur.rowcount != 1:
            raise SystemExit(f"UPDATE failed: rowcount={cur.rowcount} for experiment_id={args.experiment_id}")
        conn.commit()

        out = {
            "ok": True,
            "experiment_id": args.experiment_id,
            "operator": args.researcher_user_id,
        }
        print(json.dumps(out, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
