from __future__ import annotations


def validate_single_read_only_sql(sql: str, *, dialect: str, read_only: bool = True) -> list[str]:
    """Return deterministic syntax/safety issues for one SQL statement."""
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return []
    try:
        statements = sqlglot.parse(sql, read=dialect, error_level=sqlglot.errors.ErrorLevel.RAISE)
        if len(statements) != 1:
            return ["Exactly one SQL statement is required."]
        parsed = statements[0]
        allowed = (exp.Select, exp.Union, exp.Except, exp.Intersect)
        forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Merge)
        if read_only and (not isinstance(parsed, allowed) or parsed.find(forbidden)):
            return ["Only read-only statements are allowed. Write operations are forbidden."]
        return []
    except Exception as error:
        return [str(error)]
