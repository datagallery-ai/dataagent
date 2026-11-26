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
"""Statement parsing for the SQL checker."""

from __future__ import annotations

import sqlglot
from sqlglot import exp


def parse_artifact(sql: str, kind: str) -> exp.Expression:
    """Require exactly one CREATE TABLE or INSERT statement in the intended dialect."""
    dialect = "hive" if kind == "ddl" else "spark"
    statements = [
        item
        for item in sqlglot.parse(sql, read=dialect)
        if item is not None and not isinstance(item, exp.Semicolon)
    ]
    if len(statements) != 1:
        raise ValueError(f"{kind} must contain exactly one statement")
    statement = next(iter(statements))
    required = exp.Create if kind == "ddl" else exp.Insert
    if not isinstance(statement, required):
        raise ValueError(f"{kind} must be {required.__name__}")
    if kind == "ddl" and statement.args.get("kind", "").upper() != "TABLE":
        raise ValueError("DDL must create a table")
    return statement
