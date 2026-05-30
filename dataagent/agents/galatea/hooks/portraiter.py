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
import json
from copy import deepcopy
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from dataagent.agents.galatea.state.state import State
from dataagent.agents.galatea.utils.portraiter_utils import (
    default_user_profile,
    default_user_snapshot,
    load_user_profile,
    load_user_snapshot,
    save_user_messages_snapshot,
    save_user_profile,
    save_user_snapshot,
)
from dataagent.core.cbb.runtime import Runtime


def portraiter(state: State, runtime: Runtime) -> State:
    """Portraiter."""
    updated_state = deepcopy(state)

    user_id = state["user_id"]
    workspace_dir = Path(getattr(runtime.env, "workspace_dir", Path.cwd()))
    _save_messages_snapshot(user_id, state["messages"], workspace_dir)

    if not state.get("enable_portrait"):
        return updated_state

    user_snapshot = load_user_snapshot(user_id, workspace_dir)
    user_profile = load_user_profile(user_id, workspace_dir)
    memory = {"user_snapshot": user_snapshot, "user_profile": user_profile}
    conversation = _messages_to_conversation(state["messages"])
    updated_memory = _normalize_memory(_update_memory(memory, conversation, runtime))

    save_user_snapshot(user_id, updated_memory.get("user_snapshot", default_user_snapshot()), workspace_dir)
    save_user_profile(user_id, updated_memory.get("user_profile", default_user_profile()), workspace_dir)

    return updated_state


def _save_messages_snapshot(user_id: str, messages: list[BaseMessage], workspace_dir: Path) -> None:
    payload = {
        "messages": [
            {"type": message.__class__.__name__, "content": getattr(message, "content", "")} for message in messages
        ]
    }
    save_user_messages_snapshot(user_id, payload.get("messages", []), workspace_dir)


def _messages_to_conversation(messages: list[BaseMessage]) -> str:
    conversation = ""
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, HumanMessage):
            conversation += f"Human: {message.content}\n"
        elif isinstance(message, AIMessage):
            conversation += f"Assistant: {message.content}\n"
        elif isinstance(message, ToolMessage):
            conversation += f"Tool: {message.content}\n"
        else:
            conversation += f"Unknown: {message}\n"
    return conversation


def _default_memory() -> dict:
    return {
        "user_snapshot": {"goals": [], "constraints": [], "decisions": [], "important_findings": [], "artifacts": []},
        "user_profile": {"identity": "", "technical_level": "", "preferences": "", "recurring_topics": []},
    }


def _normalize_memory(memory: dict) -> dict:
    if not isinstance(memory, dict):
        return _default_memory()
    user_snapshot = memory.get("user_snapshot")
    user_profile = memory.get("user_profile")
    if not isinstance(user_snapshot, dict):
        user_snapshot = default_user_snapshot()
    if not isinstance(user_profile, dict):
        user_profile = default_user_profile()
    return {"user_snapshot": user_snapshot, "user_profile": user_profile}


def _update_memory(memory: dict, conversation: str, runtime: Runtime) -> dict:
    prompt = f"""You are the memory updater for an agent runtime.

Input:
- The current user snapshot JSON, wrapped between `<user_snapshot>` and `</user_snapshot>`.
- The current user profile JSON, wrapped between `<user_profile>` and `</user_profile>`.
- The recent interactions between the user and the agent, wrapped between `<conversation>` and `</conversation>`.

Task:
Update memory based on the conversation.

Rules:
- Keep everything factual and grounded in the conversation.
- Do not invent details.
- Do not include verbose logs, code, or large outputs.
- Redact obvious secrets if they appear.
- Keep the memory concise and useful for future turns.
- Output a valid JSON object only:
{{
  "user_snapshot": {{
    "...": "...",
    "...": ["..."],
    "...": [{{"...": "..."}}]
  }},
  "user_profile": {{
    "...": "...",
    "...": {{
      "...": "..."
    }},
    "...": ["..."]
  }}
}}
    - Use the provided `<user_snapshot>` and `<user_profile>` as the structural baseline.
    - Preserve existing keys when still relevant, remove stale ones, and add new keys only when clearly useful.
- Do not include any other content.

Please update memory based on:

<user_snapshot>{json.dumps(memory.get("user_snapshot", {}), ensure_ascii=False)}</user_snapshot>

<user_profile>{json.dumps(memory.get("user_profile", {}), ensure_ascii=False)}</user_profile>

<conversation>{conversation}</conversation>
"""
    llm = runtime.llm("portraiter")
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = response.content if hasattr(response, "content") else str(response)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {}
    return parsed
