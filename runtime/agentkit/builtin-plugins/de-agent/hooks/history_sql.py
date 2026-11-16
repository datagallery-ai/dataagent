"""Route local history retrieval separately from semantic metadata API recall."""

import json
from pathlib import Path

from langchain_core.messages import ToolMessage

from dataagent.history_sql import corpus_path, enabled

REFERENCE_NOTICE = (
    "历史 SQL 仅为未验证的写法参考，不是已确认业务口径。"
    "不得据此补充或覆盖用户需求、DataTaskIR 或口径冻结表；冲突时遵循用户需求与已确认 IR。"
)


def _options(context):
    return context.get_plugin_config("de-agent").get("history_sql", {})


def _error(request, status, message):
    return ToolMessage(
        content=json.dumps({"status": status, "message": message}, ensure_ascii=False),
        tool_call_id=request.tool_call["id"], status="error",
    )


def _available(context):
    options = _options(context)
    if not enabled(options):
        return None, "disabled", "历史SQL检索功能未启用；这不是已检索但无匹配。跳过引用。"
    root = corpus_path(options, context.home)
    if not root.is_dir() or not any(root.glob("*.sql")):
        return None, "not_ready", "历史SQL语料未准备或没有 SQL 文件；先运行离线下载器。"
    return root, "ready", ""


def prepare_search(request, *, params):
    """Reject disabled/unprepared searches before invoking a child model."""
    args = request.tool_call.get("args", {})
    if args.get("subagent_type") != "history-sql-recall":
        return None
    try:
        root, status, message = _available(request.runtime.context)
    except (ValueError, OSError):
        return _error(request, "configuration_error", "历史SQL语料配置无效或目录无法读取。")
    if root is None:
        return _error(request, status, message)
    description = args.get("description", "")
    return request.override(tool_call={
        **request.tool_call,
        "args": {**args, "description": (
            description + f"\n\n历史SQL语料根目录（只读）：{root}\n"
            "glob/grep/ls 必须显式指定上述 path。只读此语料目录和检索 skill。\n"
            + REFERENCE_NOTICE
        )},
    })


def guard_files(request, *, params):
    """Allow corpus, the skill and native large-result artifacts through read tools."""
    name = request.tool_call.get("name")
    if name not in {"ls", "glob", "grep", "read_file"}:
        return _error(request, "invalid_tool", "历史SQL子代理只允许文件读取与检索。")
    try:
        root, status, message = _available(request.runtime.context)
        if root is None:
            return _error(request, status, message)
        args = request.tool_call.get("args", {})
        value = args.get("file_path" if name == "read_file" else "path")
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("Use an explicit absolute corpus path")
        path = Path(value).resolve()
        skill = Path(__file__).resolve().parents[1] / "history-skills/history-sql-retrieval/SKILL.md"
        if name == "read_file" and path == skill:
            return None
        artifacts = (
            request.runtime.context.session.outputs / "artifacts/large_tool_results"
        ).resolve()
        if name == "read_file" and path.is_relative_to(artifacts) and path.is_file():
            return None  # Native filesystem tools spill large search results here.
        if not path.is_relative_to(root):
            raise ValueError("Path is outside the history corpus")
        if name == "read_file" and path.suffix.lower() != ".sql":
            raise ValueError("Only SQL corpus files may be read")
        if name == "glob":
            pattern = args.get("pattern", "")
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ValueError("Glob pattern must stay relative to corpus path")
    except (ValueError, OSError) as exc:
        return _error(request, "invalid_path", str(exc))
    return None


def reference_context(context, description: str) -> dict | None:
    """Accept only an explicit per-task reference; never add it to IR evidence."""
    request = json.loads(description)
    reference = request.get("history_sql_reference")
    if reference is None:
        return None
    if not enabled(_options(context)):
        raise ValueError("history_sql_reference supplied while history_sql is disabled")
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 2000:
        raise ValueError("history_sql_reference must be a non-empty summary of at most 2000 characters")
    return {"trust": "unverified_reference", "notice": REFERENCE_NOTICE, "summary": reference}
