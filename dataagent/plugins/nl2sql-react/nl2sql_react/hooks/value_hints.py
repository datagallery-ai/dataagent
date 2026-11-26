"""Bounded literal lookup for schema grounding, never answer generation."""

import json
import re
import time

from nl2sql_react.tools.database import _run


def literals(question):
    values = []
    for match in re.finditer(r"'((?:[^'\n]|'')+)'|\"([^\"\n]+)\"", question):
        value = (match[1] if match[1] is not None else match[2]).replace("''", "'").strip()
        if 3 <= len(value) <= 80 and not value.isdecimal() and value not in values:
            values.append(value)
        if len(values) == 6:
            break
    return values


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def lookup_sql(table, columns, terms):
    expressions = [f"SUBSTR(CAST({identifier(c)} AS TEXT), 1, 2048)" for c in columns]
    predicates = [
        f"INSTR(LOWER({expr}), LOWER('" + term.replace("'", "''") + "')) > 0"
        for expr in expressions
        for term in terms
    ]
    projection = ", ".join(f"{expr} AS {identifier(c)}" for expr, c in zip(expressions, columns))
    return (
        f"SELECT {projection} FROM {identifier(table)} WHERE "
        + " OR ".join(predicates)
        + " LIMIT 2"
    )


def collect_hints(config, source, tables, question, cancelled):
    started = time.monotonic()
    terms = literals(question)
    report = {
        "terms": terms,
        "hits": [],
        "queries": [],
        "incomplete": True,
        "limits": {
            "tables": 8,
            "columns_per_table": 12,
            "rows_per_table": 2,
            "searched_text_prefix": 2048,
            "total_seconds": 5,
            "query_seconds": 1,
            "json_chars": 6000,
        },
    }
    if not terms:
        return report
    for table in tables[:8]:
        remaining = 5 - (time.monotonic() - started)
        if remaining <= 0 or cancelled.is_set():
            break
        columns = [
            name
            for name, info in table["columns"].items()
            if any(kind in info["value_type"].upper() for kind in ("CHAR", "TEXT", "CLOB"))
        ]
        # Prefer fields explicitly mentioned in the request; retain catalog order for ties.
        columns.sort(
            key=lambda name: -int(bool(re.search(r"\b" + re.escape(name) + r"\b", question, re.I)))
        )
        columns = columns[:12]
        if not columns:
            continue
        sql = lookup_sql(table["table"], columns, terms)
        record = {"table": table["table"], "columns": columns}
        try:
            observed = _run(
                config.model_copy(
                    update={
                        "max_rows": 2,
                        "query_timeout_seconds": min(1, remaining, config.query_timeout_seconds),
                    }
                ),
                source,
                "execute_sql",
                {"sql": sql},
                cancelled,
            )
            record["status"] = "sampled"
            for row in observed["rows"]:
                for column, value in zip(observed["columns"], row):
                    if not isinstance(value, str):
                        continue
                    for term in terms:
                        offset = value.lower().find(term.lower())
                        if offset < 0:
                            continue
                        hit = {
                            "table": table["table"],
                            "column": column,
                            "literal": term,
                            "observed_excerpt": value[
                                max(0, offset - 60) : offset + len(term) + 100
                            ],
                        }
                        if hit not in report["hits"]:
                            candidate = {**report, "hits": [*report["hits"], hit]}
                            if len(json.dumps(candidate, ensure_ascii=False)) > 5000:
                                continue
                            report["hits"].append(hit)
        except Exception as error:
            # Provider error strings can contain paths; only expose the exception class.
            record.update(status="unavailable", error_type=type(error).__name__)
        report["queries"].append(record)
    report["seconds"] = round(time.monotonic() - started, 3)
    while len(json.dumps(report, ensure_ascii=False)) > 6000:
        if report["hits"]:
            report["hits"].pop()
        elif report["queries"]:
            report["queries"].pop()
        else:
            break
    return report
