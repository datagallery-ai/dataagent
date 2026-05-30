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
from typing import Any

import psycopg2
import psycopg2.extras as pg_extras
from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url

IR_SCHEMA = {
    "Query": {
        "label": "TEXT",
        "description": "TEXT",
        "user_id": "TEXT",
        "sub_id": "INTEGER",
        "session_id": "TEXT",
        "run_id": "INTEGER",
        "created_at": "TIMESTAMPTZ",
        "query": "TEXT",
        "additional_files": "JSONB",
        "history": "JSONB",
    },
    "State": {
        "label": "TEXT",
        "description": "TEXT",
        "user_id": "TEXT",
        "sub_id": "INTEGER",
        "session_id": "TEXT",
        "run_id": "INTEGER",
        "created_at": "TIMESTAMPTZ",
        "state": "TEXT",
        "history": "JSONB",
    },
    "Action": {
        "label": "TEXT",
        "description": "TEXT",
        "user_id": "TEXT",
        "sub_id": "INTEGER",
        "session_id": "TEXT",
        "run_id": "INTEGER",
        "created_at": "TIMESTAMPTZ",
        "action": "TEXT",
        "params": "JSONB",
        "output": "JSONB",
        "success": "BOOLEAN",
        "history": "JSONB",
    },
    "Knowledge": {
        "label": "TEXT",
        "description": "TEXT",
        "user_id": "TEXT",
        "sub_id": "INTEGER",
        "session_id": "TEXT",
        "run_id": "INTEGER",
        "created_at": "TIMESTAMPTZ",
        "knowledge_type": "TEXT",
        "knowledge_content": "TEXT",
        "history": "JSONB",
    },
    "Tool": {
        "label": "TEXT",
        "description": "TEXT",
        "user_id": "TEXT",
        "sub_id": "INTEGER",
        "session_id": "TEXT",
        "run_id": "INTEGER",
        "created_at": "TIMESTAMPTZ",
        "tool_params": "JSONB",
        "tool_returns": "JSONB",
        "history": "JSONB",
    },
    "Table": {
        "label": "TEXT",
        "description": "TEXT",
        "user_id": "TEXT",
        "sub_id": "INTEGER",
        "session_id": "TEXT",
        "run_id": "INTEGER",
        "created_at": "TIMESTAMPTZ",
        "path": "TEXT",
        "history": "JSONB",
    },
    "Column": {
        "label": "TEXT",
        "description": "TEXT",
        "user_id": "TEXT",
        "sub_id": "INTEGER",
        "session_id": "TEXT",
        "run_id": "INTEGER",
        "created_at": "TIMESTAMPTZ",
        "from_table": "TEXT",
        "values": "JSONB",
        "supplementary_schemas": "JSONB",
        "history": "JSONB",
    },
    "File": {
        "label": "TEXT",
        "description": "TEXT",
        "user_id": "TEXT",
        "sub_id": "INTEGER",
        "session_id": "TEXT",
        "run_id": "INTEGER",
        "created_at": "TIMESTAMPTZ",
        "path": "TEXT",
        "source": "TEXT",
        "history": "JSONB",
    },
    "Script": {
        "label": "TEXT",
        "description": "TEXT",
        "user_id": "TEXT",
        "sub_id": "INTEGER",
        "session_id": "TEXT",
        "run_id": "INTEGER",
        "created_at": "TIMESTAMPTZ",
        "script_content": "TEXT",
        "script_type": "TEXT",
        "path": "TEXT",
        "related_data_list": "JSONB",
        "history": "JSONB",
    },
    "Skill": {
        "label": "TEXT",
        "description": "TEXT",
        "user_id": "TEXT",
        "sub_id": "INTEGER",
        "session_id": "TEXT",
        "run_id": "INTEGER",
        "created_at": "TIMESTAMPTZ",
        "path": "TEXT",
        "history": "JSONB",
    },
    "IR_Edge": {
        "user_id": "TEXT",
        "sub_id": "INTEGER",
        "session_id": "TEXT",
        "run_id": "INTEGER",
        "source": "TEXT",
        "target": "TEXT",
        "relationship": "TEXT",
    },
}


def create_table(url: str) -> None:
    """
    Create a PostgreSQL table if it does not exist.

    Args:
        url (str): Database connection URL.
    """
    try:
        url_obj = make_url(url)
    except Exception as e:
        raise ValueError("Invalid database URL:") from e

    dbname = url_obj.database
    if not dbname:
        raise ValueError("Database name is required in the URL")

    admin_engine = create_engine(url_obj.set(database="postgres"))
    try:
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin_conn:
            exists = admin_conn.execute(
                text("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = :name)"), {"name": dbname}
            ).scalar()
            if not exists:
                logger.debug(f"Context_IR: Database '{dbname}' does not exist. Creating...")
                admin_conn.exec_driver_sql(f'CREATE DATABASE "{dbname}"')
                logger.debug(f"Context_IR: Database {dbname} created.")
    finally:
        admin_engine.dispose()

    engine = create_engine(url_obj)
    try:
        with engine.connect() as conn_test:
            conn_test.execute(text("SELECT 1"))

        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:

            def table_exists(name: str) -> bool:
                r = conn.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' \
                            AND lower(table_name) = lower(:t))"
                    ),
                    {"t": name},
                ).scalar()
                return bool(r)

            for table_name, schema in IR_SCHEMA.items():
                if table_exists(table_name):
                    continue

                cols = ["id SERIAL PRIMARY KEY"]
                for col_name, col_type in schema.items():
                    if col_name == "created_at":
                        cols.append(f"{col_name} {col_type} DEFAULT now()")
                    else:
                        cols.append(f"{col_name} {col_type}")

                cols_sql = ",\n                ".join(cols)
                create_table_sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" (\n{cols_sql}\n);'
                conn.exec_driver_sql(create_table_sql)
                logger.debug(f"Context_IR: Created pg table '{table_name}'.")

    except Exception as e:
        raise RuntimeError(f"Error creating/checking progress tables: {e}") from e
    finally:
        try:
            engine.dispose()
        except Exception:
            logger.warning("Failed to dispose engine cleanly.")


def get_IR_from_pg(
    url: str, user_id: str, session_id: str, run_id: int, sub_id: int
) -> dict[str, list[dict[str, Any]]]:
    """
    A demonstration of reading IR content from a PostgreSQL database.

    Args:
        url (str): Database connection URL.
        user_id (str): User ID
        session_id (str): Session ID
        run_id (int): Run Number
        sub_id (int): Sub Number

    Returns:
        dict[str, list[dict[str, Any]]]: Query results
    """
    url_obj = make_url(url)
    conn_params = {
        "dbname": url_obj.database,
        "user": url_obj.username,
        "password": url_obj.password,
        "host": url_obj.host,
        "port": url_obj.port,
    }

    categorized: dict[str, list[dict[str, Any]]] = {name: [] for name in IR_SCHEMA}

    with psycopg2.connect(**conn_params) as conn, conn.cursor(cursor_factory=pg_extras.RealDictCursor) as cur:
        for table_name, schema in IR_SCHEMA.items():
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE \
                        table_schema='public' AND lower(table_name) = lower(%s))",
                (table_name,),
            )
            exists = cur.fetchone()
            if not (exists and list(exists.values())[0]):
                continue

            cur.execute(
                f'SELECT * FROM "{table_name}" WHERE \
                        user_id = %s AND session_id = %s AND run_id = %s AND sub_id = %s',
                (user_id, session_id, run_id, sub_id),
            )
            rows = cur.fetchall()
            for row in rows:
                filtered = {k: row.get(k) for k in schema}
                categorized[table_name].append(filtered)

    return categorized


def save_IR_to_pg(url: str, node_type: str, ir_data: dict[str, Any]) -> None:
    """
    Save the IR content to the PostgreSQL database.

    Args:
        url (str): Database connection URL.
        node_type (str): Type of the node (e.g., "Query", "State", etc.)
        ir_data (dict[str, Any]): IR data to be saved
    """
    url_obj = make_url(url)
    conn_params = {
        "dbname": url_obj.database,
        "user": url_obj.username,
        "password": url_obj.password,
        "host": url_obj.host,
        "port": url_obj.port,
    }

    table_schema = IR_SCHEMA.get(node_type)
    if table_schema is None:
        raise ValueError(f"Unknown node_type: {node_type}")

    cols: list[str] = []
    vals: list[Any] = []
    for col_name, col_type in table_schema.items():
        if col_name in ir_data:
            cols.append(col_name)
            v = ir_data[col_name]
            if col_type.upper() == "JSONB":
                vals.append(pg_extras.Json(v))
            else:
                vals.append(v)

    if not cols:
        raise ValueError("No valid columns to insert for given ir_data")

    col_list = ",".join(cols)
    placeholders = ",".join(["%s"] * len(vals))
    insert_sql = f'INSERT INTO "{node_type}" ({col_list}) VALUES ({placeholders})'

    with psycopg2.connect(**conn_params) as conn, conn.cursor() as cur:
        cur.execute(insert_sql, vals)
        conn.commit()
