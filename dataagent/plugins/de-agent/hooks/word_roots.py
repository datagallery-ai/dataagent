"""Check CREATE names and ALTER ADD columns after native filesystem writes."""

import json
import logging
from dataclasses import replace
from pathlib import Path

import sqlglot
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from utils.evidence import ArtifactPathError, output_file
from utils.word_root_validator import WordRootContext
from utils.word_roots import validate_identifier_word_roots

logger = logging.getLogger(__name__)


def _names(statements, path, is_create, is_alter):
    names = []
    for statement in statements:
        if is_alter and isinstance(statement, exp.Alter) and statement.args.get("kind") == "TABLE":
            columns = []
            for action in statement.args.get("actions") or []:
                if isinstance(action, exp.ColumnDef):
                    columns.append(action.name)
                elif isinstance(action, exp.Schema):
                    columns.extend(
                        column.name
                        for column in action.expressions
                        if isinstance(column, exp.ColumnDef)
                    )
            logger.info(
                "[check_word_roots] path=%s table=%s added_columns=%s",
                path,
                statement.this.sql(dialect="hive"),
                columns,
            )
            names.extend(columns)
            continue
        if not is_create or not isinstance(statement, exp.Create) or statement.kind != "TABLE":
            continue
        schema = statement.this
        if not isinstance(schema, exp.Schema) or not isinstance(schema.this, exp.Table):
            continue
        table_name = schema.this.name
        columns = [
            column.name for column in schema.expressions if isinstance(column, exp.ColumnDef)
        ]
        # Hive partition columns live in a separate schema property.
        properties = statement.args.get("properties")
        if properties:
            for partition in properties.find_all(exp.PartitionedByProperty):
                columns.extend(column.name for column in partition.find_all(exp.ColumnDef))
        logger.info(
            "[check_word_roots] path=%s table=%s columns=%s",
            path,
            schema.this.sql(dialect="hive"),
            columns,
        )
        names.extend([table_name, *columns])
    return list(dict.fromkeys(names))


async def check_word_roots(request, result, *, params):
    """Keep writes intact; distinguish unmatched roots from unavailable validation."""
    messages = (
        (result.update or {}).get("messages", []) if isinstance(result, Command) else [result]
    )
    original = next(
        (
            message
            for message in messages
            if isinstance(message, ToolMessage)
            and message.tool_call_id == request.tool_call.get("id")
        ),
        None,
    )
    if original is not None and original.status == "error":
        return None
    value = request.tool_call.get("args", {}).get("file_path", "")
    filename = Path(value).name.lower() if value else ""
    is_create, is_alter = filename.startswith("create_"), filename.startswith("alter_")
    if not filename.endswith(".sql") or not (is_create or is_alter):
        return None
    path = value
    unavailable = ""
    try:
        path = output_file(request.runtime.context, value)
        try:
            statements = sqlglot.parse(path.read_text(encoding="utf-8"), read="hive")
        except (OSError, UnicodeError, ParseError, TokenError) as exc:
            payload = {
                "file_written": True,
                "word_roots_valid": False,
                "error_msg": f"读取或解析 DDL 失败，尚未进行词根校验：{exc}；请修正 DDL。",
            }
            status = "error"
        else:
            names = _names(statements, path, is_create, is_alter)
            if not names:
                return None
            report = await validate_identifier_word_roots(
                names, _tool_context=WordRootContext(request.runtime.context)
            )
            if not isinstance(report, dict) or type(report.get("pass")) is not bool:
                raise ValueError("词根校验返回结构异常：缺少有效的 pass。")
            if report.get("pass"):
                return None
            unavailable = report.get("error_msg") or (
                "词根校验未返回名称结果。" if not report.get("names") else ""
            )
            if not unavailable:
                payload = {
                    "file_written": True,
                    "word_roots_valid": False,
                    "report": report,
                    "error_msg": "DDL 词根校验未通过，请根据业务含义和候选词根修正 DDL。",
                }
                # Unexpected non-serializable results are unavailable validation too.
                json.dumps(payload, ensure_ascii=False)
                status = "error"
    except ArtifactPathError as exc:
        payload = {
            "file_written": True,
            "word_roots_valid": False,
            "error": "artifact_outside_outputs",
            "error_msg": str(exc),
        }
        status = "error"
    except Exception as exc:
        unavailable = f"{type(exc).__name__}: {exc}"
    if unavailable:
        warning = (
            f"DDL 词根校验未完成：{unavailable}。文件已写入；请保留候选名称继续任务，"
            "在最终交付中说明未完成词根校验，不得声称已通过，不要反复重试写入同一文件。"
        )
        logger.warning("[check_word_roots] path=%s %s", path, warning)
        payload = {"file_written": True, "word_roots_valid": None, "warning": warning}
        status = "success"
    content = json.dumps({"path": str(path), **payload}, ensure_ascii=False)
    if original is not None:
        content = f"{original.content}\n{content}"
    message = (
        original.model_copy(update={"content": content, "status": status})
        if original is not None
        else ToolMessage(content=content, tool_call_id=request.tool_call.get("id"), status=status)
    )
    if isinstance(result, Command):
        update = dict(result.update or {})
        update["messages"] = [message if item is original else item for item in messages]
        if original is None:
            update["messages"].append(message)
        return replace(result, update=update)
    return message
