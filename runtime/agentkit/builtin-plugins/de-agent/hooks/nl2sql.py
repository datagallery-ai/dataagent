"""Adapt data-engineering evidence at NL2SQL's per-task boundaries."""

import json
import re
from pathlib import Path

import sqlglot
from sqlglot import exp
from utils.checks import _validate_ddl_comprehensive
from utils.evidence import model_evidence


def prepare_input(state, runtime, *, params):
    """Prepare this task's explicit source_table selection, UDFs, target DDL and SQL rules."""
    context = runtime.context
    question, source_table = _task_arguments(state.get("question", ""))
    selected = set(source_table)
    records = model_evidence(context)
    schemas = {}
    supporting = []
    for record in records:
        kind = record.get("kind")
        if kind == "schema" and record.get("query") in selected:
            schemas[record.get("query")] = record.get("payload")
        elif kind == "udf":
            supporting.append(record)
        elif kind == "retrieve":
            tables = [
                table
                for table in record.get("payload", {}).get("tables", [])
                if table.get("table") in selected
            ]
            if tables:
                supporting.append({**record, "payload": {"tables": tables}})
        elif kind == "joins":
            queried = [table for table in record.get("query", []) if table in selected]
            if queried:
                joins = _selected_joins(record.get("payload", []), selected)
                supporting.append({**record, "query": queried, "payload": joins})
    missing = [table for table in source_table if not schemas.get(table)]
    if missing:
        raise ValueError(
            f"Missing source schema evidence for {missing}; "
            "run metadata-recall and get_table_schema for these tables first"
        )
    ddl = _select_ddl(context, question)
    errors = _validate_ddl_comprehensive(ddl)
    if errors:
        raise ValueError("Fix target DDL before generating INSERT: " + "; ".join(errors))
    rules = (
        Path(__file__).resolve().parents[1] / "skills/feature-engineering/references/SQL_RULES.md"
    ).read_text(encoding="utf-8")
    evidence = {
        "source_table": source_table,
        "target_ddl_path": str(ddl),
        "target_ddl": ddl.read_text(encoding="utf-8"),
        "semantic": supporting,
    }
    return {
        "question": question,
        "schema_str": json.dumps(
            {table: schemas.get(table) for table in source_table}, ensure_ascii=False
        ),
        "evidence": json.dumps(evidence, ensure_ascii=False),
        "sql_rules": rules + "\n只生成与目标 DDL 完全对应的一条 INSERT SQL。禁止执行 SQL。",
    }


def _task_arguments(description):
    try:
        arguments = json.loads(description)
    except (ValueError, TypeError):
        arguments = None
    if not isinstance(arguments, dict):
        raise ValueError(
            'NL2SQL task description must be JSON: {"question": "...", "source_table": ["db.table"]}'
        )
    question = arguments.get("question")
    source_table = arguments.get("source_table")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("NL2SQL task question must be a non-empty string")
    if not isinstance(source_table, list) or not source_table:
        raise ValueError(
            "NL2SQL task source_table must be a non-empty list of selected db.table names"
        )
    for table in source_table:
        if (
            not isinstance(table, str)
            or re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", table) is None
        ):
            raise ValueError("NL2SQL task source_table must contain lowercase db.table identifiers")
    return question.strip(), list(dict.fromkeys(source_table))


def _selected_joins(payload, selected):
    joins = []
    for relation in payload:
        if _endpoint_table(relation.get("src", "")) not in selected:
            continue
        targets = [
            column
            for column in relation.get("target_column", [])
            if _endpoint_table(column) in selected
        ]
        if targets:
            joins.append({**relation, "target_column": targets})
    return joins


def _endpoint_table(endpoint):
    return ".".join(endpoint.split("@", 1)[0].split(".")[:2])


def finish_output(result, runtime, *, params):
    """Save generated INSERT as a session artifact and preserve workflow failures."""
    if result.get("error"):
        return result
    sql = result.get("sql", "").strip()
    # Scheduler substitutions are not SQL tokens; normalize only the parse copy.
    parsed = sqlglot.parse(re.sub(r"\$\{[^}]+\}", "scheduler_value", sql), read="hive")
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Insert):
        raise ValueError("NL2SQL must return exactly one INSERT statement")
    target = parsed[0].this
    if isinstance(target, exp.Schema):
        target = target.this
    if not isinstance(target, exp.Table) or not target.db:
        raise ValueError("Generated INSERT must use a fully qualified target table")
    ddl = _select_ddl(runtime.context, result.get("question", ""))
    expected = _ddl_table(ddl)
    actual = f"{target.db}.{target.name}"
    if actual.casefold() != expected.casefold():
        raise ValueError(f"INSERT target {actual} differs from target DDL {expected}")
    folder = runtime.context.session.outputs / "sql"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"insert_{actual}.sql"
    path.write_text(sql + "\n", encoding="utf-8")
    return {**result, "artifact_path": str(path), "ddl_path": str(ddl)}


def _ddl_table(path):
    content = path.read_text(encoding="utf-8")
    match = re.search(
        r"CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)", content, re.I
    )
    if match is None:
        raise ValueError(f"Cannot identify target table in {path.name}")
    return match.group(1)


def _select_ddl(context, question):
    root = context.session.outputs.resolve()
    candidates = [
        path for path in root.rglob("create_*.sql") if path.resolve().is_relative_to(root)
    ]
    named = [
        path
        for path in candidates
        if re.search(rf"(?<![\w.]){re.escape(_ddl_table(path))}(?![\w.])", question)
    ]
    selected = named or candidates
    if len(selected) != 1:
        raise ValueError(
            "Write one create_*.sql, or name exactly one target table in the task description"
        )
    return selected[0]
