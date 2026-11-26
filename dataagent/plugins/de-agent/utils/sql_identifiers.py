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
"""Extract target identifiers without confusing SQL quoting with name components."""

import sqlglot
from sqlglot import exp


def target_table_parts(
    statement: exp.Expression, *, normalize_case: bool = True
) -> tuple[str, ...]:
    """Return CREATE/INSERT target components; a quoted dot stays inside its component."""
    if not isinstance(statement, (exp.Create, exp.Insert)) or (
        isinstance(statement, exp.Create) and statement.kind.upper() != "TABLE"
    ):
        raise ValueError("A CREATE TABLE or INSERT statement is required")
    target = statement.this
    if isinstance(target, exp.Schema):
        target = target.this
    if not isinstance(target, exp.Table) or not target.parts:
        raise ValueError("An explicit target table is required")
    names = tuple(part.name for part in target.parts)
    return tuple(name.casefold() for name in names) if normalize_case else names


def parse_target_table(sql: str, *, dialect: str, normalize_case: bool = True) -> tuple[str, ...]:
    """Parse exactly one statement and return its target name components."""
    statements = [
        item
        for item in sqlglot.parse(sql, read=dialect)
        if item is not None and not isinstance(item, exp.Semicolon)
    ]
    if len(statements) != 1:
        raise ValueError("Exactly one SQL statement is required")
    return target_table_parts(statements[0], normalize_case=normalize_case)
