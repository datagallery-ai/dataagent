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
import re

import pandas as pd
from sqlalchemy import create_engine, text

from dataagent.actions.tools.context import ToolExecutionContext

_FORBIDDEN_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|MERGE|GRANT|REVOKE|CALL|EXEC|EXECUTE|SET|USE)\b",
    re.IGNORECASE,
)


def _validate_readonly_sql(sql_command: str) -> str:
    sql = str(sql_command or "").strip()
    if not sql:
        raise ValueError("sql_command must not be empty.")
    if ";" in sql:
        raise ValueError("sql_command must contain a single read-only statement without semicolons.")
    if "--" in sql or "/*" in sql or "*/" in sql:
        raise ValueError("sql_command must not contain SQL comments.")
    first_token = sql.split(None, 1)[0].upper()
    if first_token not in {"SELECT", "WITH"}:
        raise ValueError("Only read-only SELECT queries are allowed.")
    if _FORBIDDEN_SQL_RE.search(sql):
        raise ValueError("Only read-only SELECT queries are allowed.")
    return sql


def load_table(sql_command: str, *, _tool_context: ToolExecutionContext) -> pd.DataFrame:
    """Load table in MySQL to pandas dataframe.

    Args:
        sql_command (str): SQL command to read table.

    Returns:
        pd.DataFrame, loaded pandas table.
    """
    cm = _tool_context.config_manager
    sql_address = cm.get("DATASOURCE.database_address")
    table_name = cm.get("DATASOURCE.database_table_name")
    if table_name is None:
        raise ValueError("DATASOURCE.database_table_name is not set, please set in the config file")
    url = f"{sql_address}/{table_name}"
    engine = create_engine(url)
    sql_command = _validate_readonly_sql(sql_command)
    df = pd.read_sql(text(sql_command), con=engine)
    return df  # 返回DataFrame格式的数据,可保存为csv等格式
