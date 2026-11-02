# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Build legacy IR inputs from user messages and successful native evidence."""

from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from dataagent.models import ModelContext
from utils.evidence import _without_diagnostics, read_evidence


def _user_text(message):
    if isinstance(message, HumanMessage):
        content = message.content
    elif isinstance(message, dict) and message.get("role", message.get("type")) in {
        "user",
        "human",
    }:
        content = message.get("content", "")
    else:
        return None
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))


def input_state(state) -> dict:
    """Keep only explicit user input, never assistant thoughts or NL2SQL descriptions."""
    users = [text for message in state.get("messages", []) if (text := _user_text(message))]
    return {
        "user_query": state.get("user_query") or (users[0] if users else ""),
        "confirmed_context": "\n\n".join(users[1:]),
    }


def result_messages(result):
    if isinstance(result, Command):
        return (result.update or {}).get("messages", [])
    return [result]


def tool_succeeded(request, result) -> bool:
    call_id = request.tool_call.get("id")
    messages = [
        item
        for item in result_messages(result)
        if isinstance(item, ToolMessage) and item.tool_call_id == call_id
    ]
    return bool(messages) and all(item.status != "error" for item in messages)



def collect_evidence(context: ModelContext, state: dict) -> list[dict]:
    """Read semantic evidence files and successful document results in native history."""
    evidence = []
    for record in read_evidence(context):
        if record.get("kind") in {"schema", "udf", "joins", "retrieve"}:
            evidence.append({
                "tool": "metadata_recall",
                "params": {"kind": record.get("kind"), "query": record.get("query")},
                "output": _without_diagnostics(record.get("payload")),
            })

    root = context.session.outputs.resolve()
    calls = {}
    for message in state.get("messages", []):
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                calls[call["id"]] = call
        elif isinstance(message, ToolMessage) and message.status != "error":
            call = calls.get(message.tool_call_id)
            if not call or call["name"] != "read_file":
                continue
            args = call.get("args", {})
            value = args.get("file_path", "")
            if not isinstance(value, str) or Path(value).suffix.lower() != ".md":
                continue
            path = (Path(value) if Path(value).is_absolute() else root / value).resolve()
            # Generated reports and IR diagnostics are not original business evidence.
            if path.is_relative_to(root) or path.is_relative_to(Path(__file__).resolve().parent):
                continue
            evidence.append({
                "tool": "read_file",
                "params": {**args, "path": str(path)},
                "output": [message.content],
            })
    return evidence
