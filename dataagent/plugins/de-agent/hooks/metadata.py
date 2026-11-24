"""Preload metadata at the native task boundary, where the original query is available."""

import json

import httpx
from langchain_core.messages import HumanMessage, ToolMessage
from utils.feedback import load_rejections
from utils.recall import load_semantic_context


def prepare_recall(request, *, params):
    """Inject cached semantic candidates before delegating metadata recall to its native child."""
    arguments = request.tool_call.get("args", {})
    if arguments.get("subagent_type") != "metadata-recall":
        return None
    query = _original_query(request.state.get("messages", []))
    try:
        if not query:
            raise ValueError("The original user query is required for metadata recall")
        ledger = load_rejections(request.runtime.context)
        expanded_query = query + "\n" + arguments.get("description", "") if ledger else query
        result = load_semantic_context(request.runtime.context, expanded_query)
    except (httpx.HTTPError, ValueError) as exc:
        return ToolMessage(
            content=(
                f"Metadata preloading failed ({type(exc).__name__}); metadata-recall was not started. "
                "No source tables have been established by this call."
            ),
            tool_call_id=request.tool_call.get("id", ""),
            name=request.tool_call.get("name"),
            status="error",
        )
    rejected = load_rejections(request.runtime.context)
    context_text = json.dumps(result.get("tables"), ensure_ascii=False)
    description = (
        arguments.get("description", "")
        + "\n\n原始用户问题（只作为检索背景）：\n"
        + query
        + "\n\n<semantic_retrieve_context>\n"
        + context_text
        + "\n</semantic_retrieve_context>\n"
        + "以上候选表已由前置 Hook 按原始用户问题召回并缓存。"
        + "按需补充 schema、UDF、Join 证据；证据不足可用不同业务词补充检索。用户已拒绝的数据源不得再次提案。"
        + ("\n用户拒绝口径：" + str(rejected.get("feedback")) if rejected else "")
    )
    return request.override(
        tool_call={**request.tool_call, "args": {**arguments, "description": description}}
    )


def _original_query(messages):
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.text.strip()
        if isinstance(message, dict) and message.get("role", message.get("type")) in {
            "user",
            "human",
        }:
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
    return ""
