"""Render SQLite queries with their explicit CTE column names intact."""

from sqlglot import exp
from sqlglot.dialects.sqlite import SQLite


class _SQLiteGenerator(SQLite.Generator):
    def tablealias_sql(self, expression: exp.TableAlias) -> str:
        # SQLite supports column lists on CTEs, while ordinary table aliases do not.
        if isinstance(expression.parent, exp.CTE) and expression.columns:
            alias = self.sql(expression, "this")
            columns = self.expressions(expression, key="columns", flat=True)
            return f"{alias}({columns})"
        return super().tablealias_sql(expression)


def render_sqlite_sql(statement: exp.Expression) -> str:
    return _SQLiteGenerator(dialect=SQLite()).generate(statement)
