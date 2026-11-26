"""Persist observed SQL results and resolve the child's explicit final selection.

Records live in native ToolMessage.artifact, isolated by the child's initial human
message ID. No shared registry, custom graph state, or model-authored file content.
Both hooks await disk I/O in a worker thread before publishing paths.
"""

import asyncio
import csv
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field

from dataagent import safe_error
from nl2sql_react.hooks.lint import lint
from nl2sql_react.tools.database import configuration, redaction_secrets

_RECORD = "nl2sql_local_result"
_TOOL = "nl2sql-react__execute_sql"
_PREVIEW_ROWS = 20


class Selection(BaseModel):
    """Model-owned interpretation only; execution facts are filled from the record."""

    status: Literal["success", "partial", "needs_clarification", "error"]
    query_id: str | None = None
    conclusion: str = ""
    definitions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    error: dict | str | None = None


def _task(state):
    initial = next(message for message in state["messages"] if isinstance(message, HumanMessage))
    if not initial.id:
        raise ValueError("The native subagent task must have an initial message ID")
    return hashlib.sha256(initial.id.encode()).hexdigest()[:32], initial.text


def _directory(context, task_id):
    return Path(context.session.outputs) / "nl2sql-local" / task_id


def _json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _lint_answer(request, observed, params):
    """Advisory checks, recorded even when they find nothing.

    The record distinguishes "not an answer", "disabled", "checked with no findings" and
    "checked with findings", so a saved artifact shows whether the feature ran at all.
    A failure here is reported, never raised: checks must not break result capture.
    """
    if params.get("purpose") != "answer":
        return {"enabled": False, "reason": "not_an_answer_execution", "findings": []}
    try:
        config, _ = configuration(request.runtime.context)
    except (ValueError, AttributeError, KeyError) as error:
        return {
            "enabled": False,
            "reason": "config_unavailable",
            "error_type": type(error).__name__,
            "findings": [],
        }
    if not config.sql_lint:
        return {"enabled": False, "reason": "disabled_by_config", "findings": []}
    question = next(
        (m.text for m in request.runtime.state["messages"] if isinstance(m, HumanMessage)), ""
    )
    try:
        findings = lint(observed.get("sql", ""), question)
    except Exception as error:
        return {
            "enabled": True,
            "reason": "check_failed",
            "error_type": type(error).__name__,
            "findings": [],
        }
    return {"enabled": True, "reason": "checked", "findings": findings}


def _write_snapshot(directory, record):
    """Publish all four files together. data.json retains NULL and numeric values."""
    parent = directory / "queries"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / record["query_id"]
    staging = parent / f".{record['query_id']}-{uuid4().hex}.tmp"
    staging.mkdir()
    names = ("query.sql", "data.json", "data.csv", "metadata.json")
    paths = [str(destination / name) for name in names]
    try:
        result = record["result"]
        (staging / "query.sql").write_text(result["sql"], encoding="utf-8")
        _json(
            staging / "data.json",
            {
                key: result[key]
                for key in (
                    "columns",
                    "rows",
                    "row_count",
                    "truncated",
                )
            },
        )
        with (staging / "data.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(result["columns"])
            writer.writerows(result["rows"])
        _json(
            staging / "metadata.json",
            {key: value for key, value in record.items() if key != "result"}
            | {key: value for key, value in result.items() if key != "rows"}
            | {
                "artifacts": paths,
                "data_note": "data.json preserves types; CSV renders NULL as empty cells.",
            },
        )
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return paths


async def capture_result(request, result, *, params):
    """Capture actual execution facts, including when visible content is offloaded."""
    if not isinstance(result, ToolMessage) or result.status == "error":
        return None
    artifact = dict(result.artifact) if isinstance(result.artifact, dict) else {}
    observed = artifact.pop("nl2sql_execution", None)
    try:
        visible = json.loads(result.text)
    except (ValueError, TypeError):
        visible = None
    if observed is None:
        observed = visible
    if not isinstance(observed, dict) or observed.get("error") is not None:
        return None
    if observed.get("operation") != "execute_sql" or "rows" not in observed:
        return None
    task_id, question = _task(request.runtime.state)
    record = {
        "query_id": uuid4().hex,
        "task_id": task_id,
        "question": question,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "result": observed,
        "artifacts": [],
        "artifact_error": None,
        "contract_id": params.get("contract_id"),
    }
    # Run advisory checks before publishing, so metadata.json records what was shown.
    record["sql_lint"] = await asyncio.to_thread(_lint_answer, request, observed, params)
    try:
        record["artifacts"] = await asyncio.to_thread(
            _write_snapshot,
            _directory(request.runtime.context, task_id),
            record,
        )
    except OSError as error:
        record["artifact_error"] = safe_error(error, redaction_secrets(request.runtime.context))
    # artifact is not model input. Keep the original observation even if the host
    # offloads/truncates the visible message, or the filesystem write failed.
    artifact[_RECORD] = record
    references = {
        key: record[key]
        for key in (
            "query_id",
            "artifacts",
            "artifact_error",
        )
    }
    references["execution_id"] = record["query_id"]
    if record["sql_lint"]["enabled"]:
        references["sql_lint"] = record["sql_lint"]
        if record["sql_lint"]["findings"]:
            references["sql_checks"] = record["sql_lint"]["findings"]
            references["sql_checks_note"] = (
                "Advisory only. These are questions about the SQL you just ran, not errors. "
                "If the request really requires what they flag, ignore them and finish. "
                "A warning alone does not authorize another execution. If observations reveal a "
                "concrete contradiction, use the bounded evidence-based repair protocol."
            )
    content = (
        json.dumps({**references, **observed}, ensure_ascii=False)
        if visible is not None
        else json.dumps(references, ensure_ascii=False) + "\n" + result.text
    )
    return result.model_copy(update={"content": content, "artifact": artifact})


def _selection(text):
    """Tolerate a single fenced/prefixed JSON object, never choose between objects."""
    decoder = json.JSONDecoder()
    objects = []
    cursor = 0
    while (start := text.find("{", cursor)) >= 0:
        try:
            value, size = decoder.raw_decode(text[start:])
        except ValueError:
            cursor = start + 1
            continue
        objects.append(value)
        cursor = start + size
    if len(objects) != 1:
        raise ValueError("Return exactly one result selection JSON object")
    return Selection.model_validate(objects[0])


def _write_final(directory, payload):
    """Only this manifest marks a snapshot as the selected answer."""
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "final.json"
    staging = directory / f".final-{uuid4().hex}.tmp"
    try:
        _json(staging, payload)
        staging.replace(destination)
    finally:
        staging.unlink(missing_ok=True)
    return str(destination)


async def build_result(state, runtime, selection, *, extra=None):
    """Validate a selection against actual executions and write the final manifest."""
    messages = state["messages"]
    task_id, question = _task(state)
    payload = {
        "status": "error",
        "query_id": None,
        "task_id": task_id,
        "source": None,
        "db_id": None,
        "question": question,
        "sql": None,
        "columns": [],
        "rows": [],
        "returned_row_count": 0,
        "truncated": None,
        "preview_truncated": False,
        "conclusion": "",
        "definitions": [],
        "assumptions": [],
        "unresolved": [],
        "artifacts": [],
        "final_manifest": None,
        "artifact_error": None,
        "error": None,
        **(extra or {}),
    }
    try:
        selection = _selection(selection) if isinstance(selection, str) else selection
        if selection.status in {"success", "partial"}:
            records = [
                m.artifact[_RECORD]
                for m in messages
                if isinstance(m, ToolMessage)
                and m.name == _TOOL
                and isinstance(m.artifact, dict)
                and _RECORD in m.artifact
            ]
            selected = next(
                (
                    record
                    for record in records
                    if record["query_id"] == selection.query_id and record["task_id"] == task_id
                ),
                None,
            )
            if selected is None or selected["result"].get("purpose") != "answer":
                raise ValueError("Select a successful purpose='answer' query_id from this task")
            if selection.error is not None:
                raise ValueError("An answer selection cannot also report an execution error")
            result = selected["result"]
            payload.update(selection.model_dump())
            payload["execution_id"] = selection.query_id
            payload.update(
                {key: result.get(key) for key in ("source", "db_id", "sql", "columns", "truncated")}
            )
            payload.update(
                rows=result["rows"][:_PREVIEW_ROWS],
                returned_row_count=result["row_count"],
                preview_truncated=len(result["rows"]) > _PREVIEW_ROWS,
                artifacts=selected["artifacts"],
                artifact_error=selected["artifact_error"],
            )
            if result["truncated"] or selected["artifact_error"] or selection.unresolved:
                payload["status"] = "partial"
            if not selected["artifact_error"]:
                try:
                    payload["final_manifest"] = await asyncio.to_thread(
                        _write_final,
                        _directory(runtime.context, task_id),
                        payload,
                    )
                except OSError as error:
                    payload["status"] = "partial"
                    payload["artifact_error"] = safe_error(
                        error, redaction_secrets(runtime.context)
                    )
        else:
            if selection.query_id is not None:
                raise ValueError("A blocked task must not select a final answer query_id")
            payload.update(selection.model_dump())
    except ValueError as error:
        # Do not send an unsupported model-authored SQL/result or stale probe to
        # the parent when the selector contract fails. No extra model round.
        payload["error"] = {
            "code": "invalid_result_selection",
            "message": safe_error(
                error,
                redaction_secrets(runtime.context),
            )["message"],
        }
    payload["execution_id"] = payload["query_id"]
    return payload


async def finalize_result(state, runtime, *, params):
    """Compatibility helper for result serialization; production uses finish_query."""
    final = next(
        (m for m in reversed(state["messages"]) if isinstance(m, AIMessage) and not m.tool_calls),
        None,
    )
    if final is None:
        return None
    payload = await build_result(state, runtime, final.text)
    return {
        "messages": [final.model_copy(update={"content": json.dumps(payload, ensure_ascii=False)})]
    }
