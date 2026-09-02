from __future__ import annotations

import json
from typing import Any


def message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


def last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return message_text(message.get("content"))
    return ""


def to_langchain_messages(messages: list[dict[str, Any]]) -> list[Any]:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    converted: list[Any] = []
    for message in messages:
        role = message.get("role")
        content = message_text(message.get("content"))
        message_id = message.get("id")
        extras = {"id": message_id} if isinstance(message_id, str) and message_id else {}
        if role == "user":
            converted.append(HumanMessage(content=content, **extras))
        elif role == "assistant":
            converted.append(AIMessage(content=content, **extras))
        elif role == "system":
            converted.append(SystemMessage(content=content, **extras))
    return converted


def resume_message(response: Any) -> str:
    if response is False or response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        for key in ("answer", "message", "text", "content"):
            value = response.get(key)
            if value is not None:
                return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return json.dumps(response, ensure_ascii=False)
    return str(response)


def chunk_text(content: Any) -> str:
    return message_text(content)
