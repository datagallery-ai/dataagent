# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Semantic-schema qualification shared by the security rules and the resolver.

Kept in its own leaf module so ``rules`` and ``semantic_resolver`` can import
it eagerly: the plugin loader drops plain ``nl2sql`` module names after
assembly, so function-level imports of plugin code fail at run time.
"""

from typing import Any

from sqlglot.errors import OptimizeError
from sqlglot.expressions import Expression
from sqlglot.optimizer.qualify import qualify


def qualify_with_semantic_schema(
    statement: Expression,
    *,
    dialect: str,
    schema: dict[str, Any],
) -> Expression:
    """Qualify table aliases, columns, and stars using semantic metadata."""
    sqlglot_schema: dict[str, Any] = {}
    for table_name, table_meta in schema.items():
        columns = dict.fromkeys(table_meta.get("columns", {}), "UNKNOWN")
        _insert_sqlglot_schema(sqlglot_schema, str(table_name), columns)
    try:
        return qualify(
            statement.copy(),
            dialect=dialect,
            schema=sqlglot_schema,
            identify=False,
            validate_qualify_columns=True,
        )
    except OptimizeError as exc:
        raise ValueError(str(exc)) from exc


def _insert_sqlglot_schema(
    target: dict[str, Any], table_name: str, columns: dict[str, str]
) -> None:
    parts = [part for part in table_name.split(".") if part]
    current = target
    for qualifier in parts[:-1]:
        child = current.get(qualifier, {})
        if not isinstance(child, dict):
            child = {}
        current.update({qualifier: child})
        current = child
    if parts:
        current.update({parts[-1]: columns})
