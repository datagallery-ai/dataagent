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
from copy import deepcopy

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.agents.galatea.state.state import State
from dataagent.core.cbb.runtime import Runtime

MIN_START, MAX_END = 6, -6
COMPRESSION_THRESHOLD = 262144


def pruner(state: State, runtime: Runtime) -> State:
    """Pruner."""
    updated_state = deepcopy(state)

    start, end = MIN_START, len(state["messages"]) + MAX_END
    while start < end:
        if isinstance(state["messages"][start], AIMessage):
            break
        start += 1
    while end > start:
        if isinstance(state["messages"][end], ToolMessage):
            break
        end -= 1

    if start >= end:
        return updated_state

    messages_to_summarize = state["messages"][start : end + 1]
    content = ""
    for message in messages_to_summarize:
        if isinstance(message, AIMessage):
            content += f"Assistant: {message}\n"
        elif isinstance(message, ToolMessage):
            content += f"Tool: {message}\n"
        else:
            content += f"Unknown: {message}\n"
    if len(content) < COMPRESSION_THRESHOLD:
        return updated_state

    summary = _summarize_messages(content, runtime)
    if summary:
        updated_state["messages"] = (
            state["messages"][:start] + [AIMessage(content=summary)] + state["messages"][end + 1 :]
        )

    return updated_state


def _summarize_messages(content: str, runtime: Runtime) -> str:
    """Summarize messages."""
    prompt = f"""You are a context compression model for an agent runtime.

Input: a chronological list of messages (assistant/tool).

Task: produce a compact, faithful summary that preserves:
- Key actions taken by the assistant.
- Key results obtained from tool executions.
- References to to external artifacts by ID/path if present.
- Open issues, errors, and TODOs.

Rule:
- Do NOT include verbose logs, code, or large outputs.
- Prefer structured YAML.
- Output only the summary (no preamble).

Please summarize the following content:
{content}
"""
    llm = runtime.llm("pruner")
    response = llm.invoke([HumanMessage(content=prompt)])
    return response.content if hasattr(response, "content") else str(response)
