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
# 本文件中的模型可见文案常量原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""给 Nemotron 3 Ultra 的八把模型键登记同一条 harness 画像。

导入时不登记。``register`` 把后缀、``read_file`` 描述覆盖和十二个补偿中间件
交给已有的 harness 注册表。这是这个模型自己的画像，不是别的模型的范例。
"""

from __future__ import annotations

import uuid
import time
import re
import json
import asyncio
from typing import Annotated as _Marked
from typing import NotRequired as _OptionalKey

import langchain.agents.middleware as _agent_middleware
import langchain.agents.middleware.types as _mw_types
import langchain_core.messages as _lc_messages
import langchain_core.messages.utils as _message_utils
from dataagent.core.profiles.harness.harness_profiles import HarnessProfile as _RuntimeProfile
from dataagent.core.profiles.harness.harness_profiles import (
    _register_harness_profile_impl as _publish,
)

ToolRetryMiddleware = _agent_middleware.ToolRetryMiddleware
AgentMiddleware = _mw_types.AgentMiddleware
AgentState = _mw_types.AgentState
ExtendedModelResponse = _mw_types.ExtendedModelResponse
ModelResponse = _mw_types.ModelResponse
PrivateStateAttr = _mw_types.PrivateStateAttr
hook_config = _mw_types.hook_config
AIMessage = _lc_messages.AIMessage
HumanMessage = _lc_messages.HumanMessage
ToolMessage = _lc_messages.ToolMessage
_openai_tool_calls = _message_utils._convert_to_openai_tool_calls
del _agent_middleware, _mw_types, _lc_messages, _message_utils

_HiddenFlag = _OptionalKey[_Marked[bool, PrivateStateAttr]]
_HiddenFlags = _OptionalKey[_Marked[list[str], PrivateStateAttr]]

_PAGE = 500
_HTTP_429 = 429
_RATE_PAUSES = (4.0, 12.0)
_HEADER_SPLIT = 3
_MIN_MESSAGES = 6
_MIN_USER_TURNS = 2
_SUMMARY_ROWS = 12
_SUMMARY_CHARS = 500
_FOLLOWUP_QUESTION_CAP = 2
_REPAIR_TURNS = 8
_REPAIR_TOOLS = 28
_LITERAL_MIN = 3
_LITERAL_MAX = 80
_HINT_LIMIT = 12
_ID_WIDTH = 4
_EMPTY_RESULT = "(empty tool result)"
_UNREACHABLE = "unreachable rate-limit retry state"

_FS_TOOLS = ("ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep")
_PATH_TOOLS = frozenset(("read_file", "write_file", "edit_file", "delete"))
_NOT_DOMAIN = frozenset((*_FS_TOOLS, "compact_conversation", "execute", "task", "write_todos"))
_VERBS = frozenset("approve archive assign activate book cancel charge close create deactivate delete disable enable escalate grant invite notify pay post publish reject refund remove reserve revoke schedule send submit terminate transfer update upgrade write".split())
_READ_ONLY = frozenset("count describe fetch find get list lookup read retrieve search".split())
_LITERAL_KEYS = frozenset(("title", "subject"))
_DULL_PREFIXES = tuple(item for item in ("no files found", "no matches found", "error:"))
_SHELL_ALIASES = {"bash": "execute", "sh": "execute", "shell": "execute"}
_TOKEN = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+")
_NUMBERED = re.compile(r"^ *\d+(?:  |\t)")

_SRC_FINAL = "nemotron_final_answer_guard"
_SRC_TRANSITION = "nemotron_transition_nudge"
_SRC_FOLLOWUP = "nemotron_followup_guard"
_SRC_ENTITY = "nemotron_entity_guard"
_SRC_ACTION = "nemotron_action_commit_nudge"
_SRC_CHAIN = "nemotron_tool_chain_nudge"
_SRC_FS = "nemotron_filesystem_request_nudge"
_SRC_DOMAIN_PREF = "nemotron_domain_tool_preference"
_SRC_DOMAIN_NUDGE = "nemotron_domain_tool_nudge"
_SRC_BUDGET = "nemotron_progress_budget"
_INTERNAL_NAMES = frozenset((
    _SRC_FINAL,
    _SRC_TRANSITION,
    _SRC_FOLLOWUP,
    _SRC_ENTITY,
    _SRC_ACTION,
    _SRC_CHAIN,
    _SRC_FS,
    _SRC_DOMAIN_PREF,
    _SRC_DOMAIN_NUDGE,
    _SRC_BUDGET,
))

_MODEL_KEYS = (
    "NVIDIA:nvidia/nemotron-3-ultra-550b-a55b", "nvidia:nvidia/nemotron-3-ultra-550b-a55b",
    "baseten:nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B", "fireworks:accounts/fireworks/models/nemotron-3-ultra-nvfp4",
    "fireworks:accounts/fireworks/models/nemotron-3-ultra-bf16", "openrouter:nvidia/nemotron-3-ultra-550b-a55b",
    "nebius:nvidia/Nemotron-3-Ultra-550b-a55b", "together:nvidia/nemotron-3-ultra-550b-a55b",
)

_RAW_HEADER = '^@@ lines (\\d+)-(\\d+)(?: of \\d+)?(?: \\| .*)? @@$'
_RAW_FUNCTION_BLOCK = '<function=([^>\\s]+)\\s*>(.*?)</function>'
_RAW_PARAMETER = '<parameter\\s+name=([^>\\s]+)\\s*>(.*?)</parameter>'
_RAW_ALT_FUNCTION = '<function>\\s*(.*?)</function>\\s*(?:</tool_call>)?'
_RAW_ALT_NAME = '<name\\s*>(.*?)</name>|<name=([^<>\\s]+)</name>'
_RAW_ALT_PARAMETER = '<parameter(?:\\s+name=([^>\\s]+))?\\s*>(.*?)</parameter>'
_RAW_ALT_INLINE = '^\\s*<?([A-Za-z_][\\w-]*)>?\\s*:\\s*(.*?)\\s*$'
_RAW_THINK = '<think\\b[^>]*>(.*?)</think>\\s*'
_RAW_VERSION = '\\bv\\d+(?:\\.\\d+)+(?:[-._A-Za-z0-9]*)?\\b'
_RAW_RECURRENCE = '\\b(daily|weekly|monthly|nightly|morning|evening|every\\s+(?:day|week|month|morning|night)|each\\s+(?:day|week|month)|at\\s+\\d{1,2}(?::\\d{2})?\\s*(?:am|pm)?)\\b'
_RAW_SCHEDULE = '\\b(day/time|timezone|time\\s*zone|cadence|frequency|schedule|what time|which day|what day|how often|when should|when do you)\\b'
_RAW_SCOPE_QUESTION = '\\b(which|what)\\s+(?:data\\s+source|source|sources|scope|folders?|inboxes|labels|senders|projects?|repositories?|systems?|services?)\\b'
_RAW_SCOPE_GIVEN = '\\b(?:my|our|the|this|these|current|all)\\s+(?:sources?|folders?|inboxes|labels|senders|projects?|repositories?|repos?|systems?|services?|workspaces?|accounts?)\\b|\\b(?:from|in|under|inside|within)\\s+(?:/[\\w./-]+|[\\w.-]+\\.[A-Za-z0-9]{1,8}|(?:the\\s+)?[\\w -]{2,80}?(?:source|folder|inbox|label|sender|project|repository|repo|system|service|workspace|account))\\b'
_RAW_ANALYSIS = '\\b(?:analy[sz]e|analysis|insight|report|dashboard)\\b'
_RAW_ANALYSIS_GOAL = '\\b(?:goal|objective|question|metric|measure|compare|trend|segment|outcome|trying to learn)\\b'
_RAW_SUPPORT = '\\b(?:customer|support|ticket|question|respond|response)\\b'
_RAW_SUPPORT_DOMAIN = '\\b(?:domain|product|service|business|industry|customers?|users?)\\b'
_RAW_DELIVERY_CONTEXT = '\\b(?:brief|summary|summaries|report|digest|recurring|daily|weekly|calendar|monitoring)\\b'
_RAW_DELIVERY_QUESTION = '\\b(?:how|where|which|what)\\b.{0,80}\\b(?:receive|send|deliver|delivery|channel|email|slack|sms|notify|notification)\\b'
_RAW_QUESTION_START = '(?im)^\\s*(?:[-*]\\s*)?(?:what|which|how|where|when|who)\\b'
_RAW_VAGUE = '^\\s*(?:done|completed|all set|handled|taken care of|finished)[.!]*\\s*$'
_RAW_EXACT_WORD = '\\b(?:reply|respond|return|answer)\\s+with\\s+(?:the\\s+)?(?:single\\s+word|one\\s+word)\\s+([A-Za-z0-9_\\-[\\]{}]+)\\b'
_RAW_EXACT_PHRASE = '\\b(?:reply|respond|return|answer)\\s+with\\s+exactly\\s*:?\\s*[\\"\']?([^\\"\'.?!\\n]{1,80})'
_RAW_EXACT_ONLY = '\\b(?:reply|respond|return|answer)\\s+with\\s+([A-Za-z0-9_\\-[\\]{}]+)\\s+only\\b'
_RAW_ACTION = '\\b(proceed|go ahead|make it happen|do it|now|please\\s+(?:cancel|book|update|upgrade|send|display|show|retrieve|start|fill|lock|charge)|i want to\\s+(?:cancel|book|update|upgrade|send)|can you please\\s+(?:cancel|book|update|upgrade|send))\\b'
_RAW_CHAINED = '\\b(?:then|and|after(?:ward)?|after that|once)\\b.{0,160}\\b(?:email|send|notify|post|message|dm|create|schedule|book|cancel|update)\\b'
_RAW_FILE_TASK = '\\b(file|files|folder|folders|directory|directories|path|paths|read_file|write_file|edit_file|grep|glob|ls|filesystem|codebase|source code)\\b'
_RAW_FS_ACCESS = '\\b(?:read|open|inspect|review|summari[sz]e|analy[sz]e|process|edit)\\b.{0,160}\\b(?:file|files|document|transcript|log|repository|repo|codebase|source|/[\\w./-]+|[\\w.-]+\\.[A-Za-z0-9]{1,8})\\b'
_RAW_NEW_TASK = '\\b(?:move on|switch(?:ing)? to|new task|different task|unrelated task|separate task|new topic|different topic|unrelated topic)\\b'
_RAW_LARGE_READ = '\\b(?:read|summari[sz]e|inspect|analy[sz]e|review|process)\\b.{0,120}\\b(?:file|document|transcript|log|repository|codebase|source|/[\\w./-]+|[\\w.-]+\\.[A-Za-z0-9]{1,8})\\b'
_RAW_FILE_REF = '(?:/[\\w./-]+|\\b[\\w.-]+\\.[A-Za-z0-9]{1,8}\\b)'
_RAW_FOLLOW_ON = '\\b(?:do the same|same thing|another|next|also|continue|again)\\b.{0,120}\\b(?:file|document|transcript|log|repository|repo|codebase|source|/[\\w./-]+|[\\w.-]+\\.[A-Za-z0-9]{1,8})\\b'
_HEADER = re.compile(_RAW_HEADER)
_FUNCTION_BLOCK = re.compile(_RAW_FUNCTION_BLOCK, re.DOTALL)
_PARAMETER = re.compile(_RAW_PARAMETER, re.DOTALL)
_ALT_FUNCTION = re.compile(_RAW_ALT_FUNCTION, re.IGNORECASE | re.DOTALL)
_ALT_NAME = re.compile(_RAW_ALT_NAME, re.IGNORECASE | re.DOTALL)
_ALT_PARAMETER = re.compile(_RAW_ALT_PARAMETER, re.IGNORECASE | re.DOTALL)
_ALT_INLINE = re.compile(_RAW_ALT_INLINE, re.DOTALL)
_THINK = re.compile(_RAW_THINK, re.IGNORECASE | re.DOTALL)
_VERSION = re.compile(_RAW_VERSION)
_RECURRENCE = re.compile(_RAW_RECURRENCE, re.IGNORECASE)
_SCHEDULE = re.compile(_RAW_SCHEDULE, re.IGNORECASE)
_SCOPE_QUESTION = re.compile(_RAW_SCOPE_QUESTION, re.IGNORECASE)
_SCOPE_GIVEN = re.compile(_RAW_SCOPE_GIVEN, re.IGNORECASE)
_ANALYSIS = re.compile(_RAW_ANALYSIS, re.IGNORECASE)
_ANALYSIS_GOAL = re.compile(_RAW_ANALYSIS_GOAL, re.IGNORECASE)
_SUPPORT = re.compile(_RAW_SUPPORT, re.IGNORECASE)
_SUPPORT_DOMAIN = re.compile(_RAW_SUPPORT_DOMAIN, re.IGNORECASE)
_DELIVERY_CONTEXT = re.compile(_RAW_DELIVERY_CONTEXT, re.IGNORECASE)
_DELIVERY_QUESTION = re.compile(_RAW_DELIVERY_QUESTION, re.IGNORECASE)
_QUESTION_START = re.compile(_RAW_QUESTION_START)
_VAGUE = re.compile(_RAW_VAGUE, re.IGNORECASE)
_EXACT_WORD = re.compile(_RAW_EXACT_WORD, re.IGNORECASE)
_EXACT_PHRASE = re.compile(_RAW_EXACT_PHRASE, re.IGNORECASE)
_EXACT_ONLY = re.compile(_RAW_EXACT_ONLY, re.IGNORECASE)
_ACTION = re.compile(_RAW_ACTION, re.IGNORECASE)
_CHAINED = re.compile(_RAW_CHAINED, re.IGNORECASE)
_FILE_TASK = re.compile(_RAW_FILE_TASK, re.IGNORECASE)
_FS_ACCESS = re.compile(_RAW_FS_ACCESS, re.IGNORECASE)
_NEW_TASK = re.compile(_RAW_NEW_TASK, re.IGNORECASE)
_LARGE_READ = re.compile(_RAW_LARGE_READ, re.IGNORECASE)
_FILE_REF = re.compile(_RAW_FILE_REF)
_FOLLOW_ON = re.compile(_RAW_FOLLOW_ON, re.IGNORECASE)

_PROMPT_SUFFIX = """\
<approach>
Plan briefly before acting. When several reads or lookups are independent, issue them as parallel tool calls rather than one at a time.
</approach>

<grounding>
Verify state with tools instead of recalling it. Read files before describing
them, use lookup tools for identifiers, and use mutation tools before saying a
requested change is done.
</grounding>

<loop_control>
If a tool call fails, read the error and change the call before retrying; never
re-issue the same failing call unchanged. If a command times out or the same
error repeats, reduce the input, add a termination condition, or switch
approaches before trying again.
</loop_control>

<tool_selection>
Use filesystem tools only for file, path, repository-content, or document
questions. For API, operational, business-object, or other domain questions,
prefer the task-specific non-filesystem tools. For ranking, counting, "which",
or "most" questions over domain entities, enumerate or search candidate
entities with domain tools, fetch the relevant details or counts with matching
domain tools, compare the observed tool results, and answer from that
comparison.
</tool_selection>

<state_changes>
If the user asks to book, cancel, update, send, notify, create, or otherwise
change external state, the change is complete only after the relevant tool call
succeeds. Do not merely describe the intended action or ask the user to assume it
happened. After a successful mutation, use the tool result as the source of truth
for the final answer.
</state_changes>

<final_answer_completeness>
After tool calls succeed, the final answer must include the concrete result, not
just "done". Preserve short exact literals that identify the completed action,
especially versions, titles, and subjects from the user's request or successful
mutation tool arguments/results. If you used an opaque entity ID and an obvious
name or detail lookup tool is available, resolve the ID to human-readable
details before answering. If the user asked multiple questions, answer each one
from its matching tool output; do not substitute an entity from another subtask.
</final_answer_completeness>

<followup_defaults>
Ask follow-up questions only for information needed to proceed safely or
correctly. Do not re-ask for constraints the user already gave. For broad
analysis requests, ask for both the data source and the analysis goal before
using tools. For recurring reports, summaries, monitoring, or support workflows,
treat a stated cadence as sufficient and ask only for missing content, source,
threshold, delivery, or domain details needed to perform the task.
</followup_defaults>

<context_compaction>
If a long conversation switches to a completely unrelated new task and the
compact_conversation tool is available, call compact_conversation before starting
the new task. Also call compact_conversation before reading or summarizing a
large new file after a long conversation.
</context_compaction>"""
_READ_OVERRIDE = """\
Reads a file from the filesystem.

Use this tool for text files, source files, documents, images, audio, video, and PDFs.
If the user asks to read, inspect, review, or summarize an entire/whole/full file,
keep reading paginated chunks until you reach EOF or a tool result says the offset
exceeds the file length. A result that contains exactly `limit` numbered source
lines is only one page; continue with `offset + limit` before giving a final
whole-file answer. Use smaller `limit` values for large files to allow automatic
conversation summarization to keep context manageable.

Arguments:
- `file_path`: absolute path to the file.
- `offset`: 0-indexed source line to start from; use for pagination.
- `limit`: maximum source lines to read; use for pagination.

Results are returned with line numbers. Lines longer than 5,000 characters may
be split with continuation markers. Always read a file before editing it."""

_DOMAIN_LEAD = """\
This is not a file or repository-content request. Start with the task-specific non-filesystem tools instead of ls, glob, grep, or read_file. For ranking, counting, 'which', or 'most' questions, enumerate or search for candidate entities with the available domain tools, fetch the relevant details or counts, compare those observed results, and then answer."""
_FS_NUDGE = """\
The user is asking for file or path content, and filesystem tools are available. Do not answer that you lack access before trying the tools. If the user named a file or path, first call read_file with that path and the requested pagination/limit. If that fails or the location is ambiguous, use ls or glob to locate the file, then continue reading until the request is satisfied."""
_TRANSITION_TEXT = """\
This is a long conversation and the latest user request appears to start a new task or substantial follow-on file work. If compact_conversation is available, call it before starting the new work so prior context is compressed instead of carried forward verbatim."""
_ACTION_TEXT = """\
The user is asking you to perform an action now. If the conversation or previous tool results already provide the required identifiers, payment/source details, recipients, or parameters, call the relevant state-changing/API tool instead of replying only with policy explanation or another confirmation request. Ask exactly one missing-field question only if a required argument is still unavailable."""
_CHAIN_TEXT = """\
The user's request has a chained action after the information lookup. Use the tool results already gathered as the summary source, then call the requested state-changing tool such as email, send, notify, post, create, schedule, book, cancel, or update. Do not repeat the same lookup unless a required argument for the action is still missing."""
_DOMAIN_DONE_TEXT = """\
The filesystem search did not find useful files. Continue with the available non-filesystem API/domain tools instead of grepping or listing more files. For lookup, ranking, counting, or 'most' questions, enumerate or search for candidate entities with domain tools, fetch details or counts with the matching domain tools, compare the observed results, and answer from those results."""
_FOLLOWUP_TEXT = """\
Rewrite your follow-up so it asks for the smallest useful missing information. Do not re-ask about schedule, cadence, source, or scope when those are already supplied. For vague analysis requests, ask for both the data source and the analysis goal. For support or customer-response improvement requests, ask about the product/domain and the current support surface. For recurring briefs, reports, or monitoring requests with a stated cadence, ask for the missing delivery channel or content/source detail, not the day/time again."""

_CURRENT_ID = re.compile(r"get_current_([a-z_]+)_id")
_NAME_TITLE = re.compile(r"get_[a-z_]+_(?:name|title)")
_RELATION = re.compile(r"get_([a-z_]+)_([a-z_]+)")
_DISPLAY_GETTER = re.compile(r"get_([a-z_]+)_(?:name|title)")
_ANY_GET = re.compile(r"get_([a-z_]+)_[a-z_]+")
_OPAQUE = re.compile(rf"\b\d{{{_ID_WIDTH},}}\b")
_STEP_LOOKUP = re.compile(r"([a-z_]+) lookup for current ([a-z_]+) (\d+)")
_STEP_RELATION = re.compile(r"([a-z_]+)_id (\d+) from (?:current )?([a-z_]+) (\d+)")
_STEP_DISPLAY = re.compile(r"([a-z_]+)_id (\d+) title/name")
_ASKS_ENTITY = r"\b(?:which|what)\s+(?:\w+\s+){0,4}"


def _text_of(message):
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict):
                chunks.append(part.get("text", ""))
        return "".join(chunks)
    return ""


def _outside_user(message):
    return isinstance(message, HumanMessage) and getattr(message, "name", None) not in _INTERNAL_NAMES


def _since_last_user(messages):
    index = None
    for cursor, message in enumerate(messages):
        if _outside_user(message):
            index = cursor
    if index is None:
        return messages
    return messages[index:]


def _latest_user_text(messages):
    chosen = ""
    seen = False
    for message in messages:
        if _outside_user(message):
            chosen = _text_of(message)
            seen = True
    return chosen if seen else ""


def _outside_indexes(messages):
    return [cursor for cursor, message in enumerate(messages) if _outside_user(message)]


def _question_total(text):
    marks = text.count("?")
    starts = len(_QUESTION_START.findall(text))
    return marks if marks > starts else starts


def _squash_reply(text):
    trimmed = text.strip().strip("\"'`").strip(" .!?")
    return re.sub(r"\s+", " ", trimmed).strip()


def _wanted_exact(user_text):
    found = []
    for pattern in (_EXACT_WORD, _EXACT_PHRASE, _EXACT_ONLY):
        for match in pattern.finditer(user_text):
            found.append(_squash_reply(match.group(1)))
    return [item for item in dict.fromkeys(found) if item]


def _exact_reply_met(user_text, final_text):
    wanted = _wanted_exact(user_text)
    if not wanted:
        return False
    normalized = _squash_reply(final_text)
    return any(normalized == expected for expected in wanted)


def _is_final(message):
    if getattr(message, "name", None) == _SRC_BUDGET:
        return False
    if message.tool_calls:
        return False
    return bool(_text_of(message).strip())


def _human_nudge(content, source, **fields):
    note = HumanMessage(content=content, name=source)
    return {"messages": [note], **fields}


def _flat_tool_calls(messages):
    calls = []
    for message in messages:
        if isinstance(message, AIMessage):
            calls.extend(message.tool_calls or [])
    return calls


def _is_domain(name):
    if not name or name in _NOT_DOMAIN:
        return False
    return not name.startswith("__")


def _is_mutation(name):
    if not _is_domain(name):
        return False
    tokens = _TOKEN.findall(name.replace("_", " "))
    if not tokens:
        return False
    lowered = [token.lower() for token in tokens]
    if lowered[0] in _READ_ONLY:
        return False
    return any(token in _VERBS for token in lowered)


def _tool_label(tool):
    if isinstance(tool, dict):
        name = tool.get("name")
        if isinstance(name, str):
            return name
        function = tool.get("function")
        if isinstance(function, dict):
            nested = function.get("name")
            if isinstance(nested, str):
                return nested
        return None
    name = getattr(tool, "name", None)
    if isinstance(name, str):
        return name
    return None


def _names_on_request(request):
    labels = []
    for tool in request.tools or []:
        name = _tool_label(tool)
        if name:
            labels.append(name)
    return labels


def _ai_total(messages):
    return sum(isinstance(message, AIMessage) for message in messages)


def _tool_total(messages):
    return sum(isinstance(message, ToolMessage) for message in messages)


def _fingerprint(call):
    name = call.get("name", "")
    args = call.get("args") or {}
    payload = json.dumps(args, sort_keys=True, default=str)
    return f"{name}:{payload}"


def _longest_repeat(messages):
    best = 0
    previous = None
    run = 0
    for call in _flat_tool_calls(messages):
        signature = _fingerprint(call)
        if signature == previous:
            run += 1
        else:
            previous = signature
            run = 1
        if run > best:
            best = run
    return best


def _repair_risk(messages):
    return _ai_total(messages) >= _REPAIR_TURNS or _tool_total(messages) >= _REPAIR_TOOLS


def _mutation_literals(calls):
    found = []
    for call in calls:
        if not _is_mutation(call.get("name", "")):
            continue
        args = call.get("args") or {}
        for value in args.values():
            if isinstance(value, str):
                found.extend(_VERSION.findall(value))
        for key in _LITERAL_KEYS:
            value = args.get(key)
            if isinstance(value, str) and _LITERAL_MIN <= len(value) <= _LITERAL_MAX:
                found.append(value)
    return list(dict.fromkeys(found))


def _json_or_text(text):
    payload = text.strip()
    if payload == "":
        return None
    try:
        loaded = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return payload
    return loaded


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _paired_results(messages):
    calls = {}
    ai_messages = [message for message in messages if isinstance(message, AIMessage)]
    for message in ai_messages:
        for call in message.tool_calls or ():
            call_id = call.get("id")
            if isinstance(call_id, str):
                calls[call_id] = call
    paired = []
    for message in messages:
        if isinstance(message, ToolMessage) and message.tool_call_id in calls:
            paired.append((calls[message.tool_call_id], _json_or_text(_text_of(message))))
    return paired


def _latest_mutation(messages):
    for call, value in reversed(_paired_results(messages)):
        if _is_mutation(call.get("name", "")):
            return call, value
    return None


def _clip(value):
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True, default=str)
    elif value is None:
        text = ""
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _SUMMARY_CHARS:
        return text[: _SUMMARY_CHARS - 3].rstrip() + "..."
    return text


def _useful(value):
    text = _clip(value).lower()
    if not text:
        return False
    return not any(text.startswith(prefix) for prefix in _DULL_PREFIXES)


def _budget_paragraph(messages, reason):
    results = _paired_results(messages)
    if not results:
        return f"I could not complete this reliably within the harness step budget ({reason})."
    useful = [entry for entry in results if _useful(entry[1])]
    if len(useful) < _SUMMARY_ROWS:
        useful.extend(entry for entry in results if entry not in useful)
    seen = set()
    rows = []
    for call, value in useful[:_SUMMARY_ROWS]:
        name = call.get("name") or "tool"
        text = _clip(value)
        marker = (name, text)
        if marker in seen:
            continue
        seen.add(marker)
        rows.append(f"- {name}: {text}")
    heading = "Using the tool results gathered so far:"
    return heading + "\n" + "\n".join(rows)


def _messages_for_request(request):
    state = request.state or {}
    stored = state.get("messages") if isinstance(state, dict) else None
    if isinstance(stored, list):
        return stored
    return list(request.messages or [])


def _content_blank(content):
    if content is None or content == "":
        return True
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            return False
        if block.get("text"):
            return False
    return True


def _rate_limited(exc):
    label = type(exc).__name__.lower()
    if "ratelimit" in label or "rate_limit" in label:
        return True
    if getattr(exc, "status_code", None) == _HTTP_429:
        return True
    lowered = str(exc).lower()
    if "rate limit" in lowered:
        return True
    return "rate-limit" in str(exc).lower()


def _fresh_call(name, args):
    return {"name": name, "args": args, "id": uuid.uuid4().hex, "type": "tool_call"}


def _name_allowed(name, allowed):
    return allowed is None or name in allowed


def _alt_name(body):
    match = _ALT_NAME.search(body)
    if match is None:
        return ""
    picked = match.group(1) or match.group(2) or ""
    return picked.strip().strip("\"'")


def _alt_args(body):
    args = {}
    for match in _ALT_PARAMETER.finditer(body):
        name = (match.group(1) or "").strip().strip("\"'")
        raw = match.group(2).strip()
        if name:
            args[name] = raw
            continue
        inline = _ALT_INLINE.match(raw)
        if inline is not None:
            args[inline.group(1)] = inline.group(2).strip()
    return args


def _alternate_calls(content, allowed):
    if allowed is None:
        return [], content
    calls = []
    for block in _ALT_FUNCTION.finditer(content):
        body = block.group(1)
        name = _alt_name(body)
        if not name or name not in allowed:
            continue
        calls.append(_fresh_call(name, _alt_args(body)))
    if not calls:
        return [], content
    leftover = _ALT_FUNCTION.sub("", content).replace("</tool_call>", "").strip()
    return calls, leftover


def _primary_calls(content, allowed):
    calls = []
    for block in _FUNCTION_BLOCK.finditer(content):
        name = block.group(1).strip("\"'")
        if not _name_allowed(name, allowed):
            continue
        args = {}
        for param in _PARAMETER.finditer(block.group(2)):
            args[param.group(1).strip("\"'")] = param.group(2).strip()
        calls.append(_fresh_call(name, args))
    if not calls:
        return _alternate_calls(content, allowed)
    leftover = _FUNCTION_BLOCK.sub("", content).replace("</tool_call>", "").strip()
    return calls, leftover


def _json_calls(content, allowed):
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(content[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []
    raw_name = parsed.get("tool")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return []
    stripped = raw_name.strip()
    name = _SHELL_ALIASES.get(stripped.lower(), stripped)
    if not _name_allowed(name, allowed):
        return []
    supplied = parsed.get("args")
    if isinstance(supplied, dict):
        args = supplied
    else:
        command = parsed.get("cmd") or parsed.get("command")
        args = {"command": command} if isinstance(command, str) else {}
    return [_fresh_call(name, args)]


def _without_think_tags(message):
    content = message.content
    if not isinstance(content, str) or "</think>" not in content.lower():
        return message
    pieces = []
    for match in _THINK.finditer(content):
        chunk = match.group(1).strip()
        if chunk:
            pieces.append(chunk)
    cleaned = _THINK.sub("", content).strip()
    if cleaned == content:
        return message
    extra = dict(message.additional_kwargs)
    if pieces and "reasoning_content" not in extra:
        extra["reasoning_content"] = "\n\n".join(pieces)
    return message.model_copy(update={"content": cleaned, "additional_kwargs": extra})


def _map_response(response, convert):
    rewritten = []
    for message in response.result:
        if isinstance(message, AIMessage):
            rewritten.append(convert(message))
        else:
            rewritten.append(message)
    return ModelResponse(result=rewritten, structured_response=response.structured_response)


def _map_model_result(result, convert):
    if isinstance(result, ExtendedModelResponse):
        updated = _map_response(result.model_response, convert)
        return ExtendedModelResponse(model_response=updated, command=result.command)
    if isinstance(result, AIMessage):
        return convert(result)
    if isinstance(result, ModelResponse):
        return _map_response(result, convert)
    return result


def _repair_tool_message(message, allowed):
    if message.tool_calls:
        return message
    message = _without_think_tags(message)
    text = _text_of(message)
    calls, leftover = _primary_calls(text, allowed)
    if not calls:
        parsed = _json_calls(text, allowed)
        if parsed:
            calls, leftover = parsed, ""
    if not calls:
        return message
    return message.model_copy(update={"tool_calls": calls, "content": leftover})


def _page_number(value, default):
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _single_user(request):
    window = _since_last_user(_messages_for_request(request))
    if len(window) != 1 or not isinstance(window[0], HumanMessage):
        return None
    return window[0]


def _prefer_domain(request):
    user = _single_user(request)
    if user is None or _FILE_TASK.search(_text_of(user)):
        return False
    names = _names_on_request(request)
    has_fs = any(name in _FS_TOOLS for name in names)
    has_domain = any(_is_domain(name) for name in names)
    return has_fs and has_domain


def _prefer_filesystem(request):
    user = _single_user(request)
    if user is None or _FS_ACCESS.search(_text_of(user)) is None:
        return False
    names = set(_names_on_request(request))
    return "read_file" in names and bool(names.intersection(_FS_TOOLS))


def _append_note(request, content, source):
    note = HumanMessage(content=content, name=source)
    return request.override(messages=[*request.messages, note])


def _should_compact(messages):
    if len(messages) < _MIN_MESSAGES:
        return False
    indexes = _outside_indexes(messages)
    if len(indexes) < _MIN_USER_TURNS:
        return False
    latest = indexes[-1]
    user_text = _text_of(messages[latest])
    later = _flat_tool_calls(messages[latest:])
    if any(call.get("name") == "compact_conversation" for call in later):
        return False
    prior = _flat_tool_calls(messages[:latest])
    had_files = any(call.get("name") in _FS_TOOLS for call in prior)
    new_task = _NEW_TASK.search(user_text) is not None
    follow = _FOLLOW_ON.search(user_text) is not None
    large = _LARGE_READ.search(user_text) is not None
    refers = _FILE_REF.search(user_text) is not None
    return new_task or (had_files and (follow or large or refers))


def _transition_update(state):
    if state.get("nemotron_transition_nudged"):
        return None
    messages = list(state.get("messages") or [])
    if not _should_compact(messages):
        return None
    return _human_nudge(_TRANSITION_TEXT, _SRC_TRANSITION, nemotron_transition_nudged=True)


def _action_update(state):
    messages = list(state.get("messages") or [])
    if not messages or not isinstance(messages[-1], HumanMessage):
        return None
    last = messages[-1]
    if getattr(last, "name", None) in _INTERNAL_NAMES:
        return None
    text = _text_of(last)
    if _ACTION.search(text) is None or not _flat_tool_calls(messages[:-1]):
        return None
    key = f"{len(messages)}:{text[:200]}"
    nudged = list(state.get("nemotron_action_nudged") or [])
    if key in nudged:
        return None
    return _human_nudge(_ACTION_TEXT, _SRC_ACTION, nemotron_action_nudged=[*nudged, key])


def _call_for_tool_message(messages, tool_message):
    for call in reversed(_flat_tool_calls(messages)):
        if call.get("id") == tool_message.tool_call_id:
            return call
    return None


def _chain_update(state):
    if state.get("nemotron_tool_chain_nudged"):
        return None
    messages = list(state.get("messages") or [])
    if not messages or not isinstance(messages[-1], ToolMessage):
        return None
    window = _since_last_user(messages)
    if _CHAINED.search(_latest_user_text(window)) is None:
        return None
    calls = _flat_tool_calls(window)
    if not calls or any(_is_mutation(call.get("name", "")) for call in calls):
        return None
    return _human_nudge(_CHAIN_TEXT, _SRC_CHAIN, nemotron_tool_chain_nudged=True)


def _domain_done_update(state):
    messages = list(state.get("messages") or [])
    blocked = state.get("nemotron_domain_tool_nudged") or not messages
    if blocked or not isinstance(messages[-1], ToolMessage):
        return None
    last = messages[-1]
    lowered = _text_of(last).lower()
    if not any(lowered.startswith(prefix) for prefix in _DULL_PREFIXES):
        return None
    window = _since_last_user(messages)
    last_call = _call_for_tool_message(window, last)
    if last_call is None or last_call.get("name") not in _FS_TOOLS:
        return None
    if not any(_is_domain(call.get("name", "")) for call in _flat_tool_calls(window)):
        return None
    return _human_nudge(_DOMAIN_DONE_TEXT, _SRC_DOMAIN_NUDGE, nemotron_domain_tool_nudged=True)


def _merge_updates(updates):
    pending = [item for item in updates if item is not None]
    if not pending:
        return None
    messages = []
    fields = {}
    for item in pending:
        messages.extend(item.get("messages", []))
        for key, value in item.items():
            if key != "messages":
                fields[key] = value
    return {"messages": messages, **fields}


def _followup_rewrite(user_text, final_text):
    user_lower = user_text.lower()
    recurring = _RECURRENCE.search(user_text) is not None
    asks_schedule = _SCHEDULE.search(final_text) is not None
    too_many = recurring and _question_total(final_text) > _FOLLOWUP_QUESTION_CAP
    asks_scope = _SCOPE_QUESTION.search(final_text) is not None
    scope_given = _SCOPE_GIVEN.search(user_text) is not None
    analysis_gap = (
        _ANALYSIS.search(user_text) is not None
        and "data" in user_lower
        and _ANALYSIS_GOAL.search(final_text) is None
    )
    support_gap = _SUPPORT.search(user_text) is not None and _SUPPORT_DOMAIN.search(final_text) is None
    delivery = _DELIVERY_CONTEXT.search(user_text) is not None
    asks_delivery = _DELIVERY_QUESTION.search(final_text) is not None or asks_scope
    recurring_gap = delivery and recurring and not asks_delivery
    return (
        (recurring and asks_schedule)
        or too_many
        or (asks_scope and scope_given)
        or analysis_gap
        or support_gap
        or recurring_gap
    )


def _followup_update(state):
    messages = list(state.get("messages") or [])
    if state.get("nemotron_followup_guard_fired") or _repair_risk(messages) or not messages:
        return None
    last = messages[-1]
    if not isinstance(last, AIMessage) or not _is_final(last):
        return None
    user_text = _latest_user_text(messages[:-1])
    final_text = _text_of(last)
    if _exact_reply_met(user_text, final_text) or not _followup_rewrite(user_text, final_text):
        return None
    return _human_nudge(
        _FOLLOWUP_TEXT,
        _SRC_FOLLOWUP,
        jump_to="model",
        nemotron_followup_guard_fired=True,
    )


def _current_ids(messages):
    found = {}
    for call, value in _paired_results(messages):
        name = call.get("name")
        if not isinstance(name, str):
            continue
        match = _CURRENT_ID.fullmatch(name)
        if match is None:
            continue
        number = _as_int(value)
        if number is not None:
            found[match.group(1)] = number
    return found


def _bindings(messages):
    found = {}
    for call, value in _paired_results(messages):
        name = call.get("name", "")
        if not isinstance(name, str) or _NAME_TITLE.fullmatch(name):
            continue
        match = _RELATION.fullmatch(name)
        if match is None:
            continue
        source, target = match.groups()
        source_id = _as_int((call.get("args") or {}).get(f"{source}_id"))
        target_id = _as_int(value)
        if source_id is not None and target_id is not None:
            found[(source, source_id, target)] = target_id
    return found


def _shown_ids(messages):
    shown = set()
    for call in _flat_tool_calls(messages):
        name = call.get("name", "")
        match = _DISPLAY_GETTER.fullmatch(name)
        if match is None:
            continue
        entity = match.group(1)
        entity_id = _as_int((call.get("args") or {}).get(f"{entity}_id"))
        if entity_id is not None:
            shown.add((entity, entity_id))
    return shown


def _missing_current(messages, user_text, final_text):
    user_lower = user_text.lower()
    current = _current_ids(messages)
    if not current:
        return []
    linked = _bindings(messages)
    shown = _shown_ids(messages)
    final_numbers = set(_OPAQUE.findall(final_text))
    missing = []
    for source, source_id in current.items():
        if f"current {source}" not in user_lower:
            continue
        targets = set()
        for relation_source, _ignored, target in linked:
            if relation_source != source:
                continue
            if re.search(rf"\b{re.escape(target)}\b", user_lower):
                targets.add(target)
        for target in sorted(targets):
            target_id = linked.get((source, source_id, target))
            if target_id is None:
                missing.append(f"{target} lookup for current {source} {source_id}")
            elif (target, target_id) not in shown:
                missing.append(f"{target}_id {target_id} from current {source} {source_id}")
    for (source, source_id, target), target_id in linked.items():
        if str(target_id) in final_numbers and (target, target_id) not in shown:
            missing.append(f"{target}_id {target_id} from {source} {source_id}")
    return list(dict.fromkeys(missing))


def _missing_display(messages, user_text, final_text):
    missing = []
    shown = _shown_ids(messages)
    final_numbers = set(_OPAQUE.findall(final_text))
    user_lower = user_text.lower()
    for call in _flat_tool_calls(messages):
        name = call.get("name", "")
        if not isinstance(name, str):
            continue
        match = _ANY_GET.fullmatch(name)
        if match is None or _NAME_TITLE.fullmatch(name):
            continue
        entity = match.group(1)
        asks = re.search(_ASKS_ENTITY + re.escape(entity) + r"\b", user_lower) is not None
        mentions = (
            f"that {entity}" in user_lower
            or f"{entity} with" in user_lower
            or f"{entity} whose" in user_lower
            or f"selected {entity}" in user_lower
        )
        if not asks and not mentions:
            continue
        entity_id = _as_int((call.get("args") or {}).get(f"{entity}_id"))
        if entity_id is None:
            continue
        visible = str(entity_id) in final_numbers
        if (visible or mentions) and (entity, entity_id) not in shown:
            missing.append(f"{entity}_id {entity_id} title/name")
    return list(dict.fromkeys(missing))


def _missing_resolutions(messages, user_text, final_text):
    missing = _missing_current(messages, user_text, final_text)
    missing.extend(_missing_display(messages, user_text, final_text))
    return list(dict.fromkeys(missing))


def _resolution_steps(missing):
    lines = []
    for item in missing:
        lookup = _STEP_LOOKUP.fullmatch(item)
        if lookup is not None:
            target, source, source_id = lookup.groups()
            lines.append(f"call get_{source}_{target} with {source}_id={source_id}")
            continue
        relation = _STEP_RELATION.fullmatch(item)
        if relation is not None:
            entity, entity_id, _source, _source_id = relation.groups()
            lines.append(
                f"call get_{entity}_title or get_{entity}_name with {entity}_id={entity_id}, whichever tool exists"
            )
            continue
        display = _STEP_DISPLAY.fullmatch(item)
        if display is not None:
            entity, entity_id = display.groups()
            lines.append(
                f"call get_{entity}_title or get_{entity}_name with {entity}_id={entity_id}, whichever tool exists"
            )
    return "; ".join(dict.fromkeys(lines))


def _entity_before(state):
    if state.get("nemotron_entity_pre_nudged"):
        return None
    messages = list(state.get("messages") or [])
    if not messages or not isinstance(messages[-1], ToolMessage):
        return None
    user_text = _latest_user_text(messages)
    missing = _missing_resolutions(messages, user_text, "")
    if not missing:
        return None
    steps = _resolution_steps(missing)
    joined = ", ".join(missing)
    content = (
        "Before answering, resolve each current-entity or ID branch with its "
        "own lookup result instead of reusing another branch's entity. "
        f"Missing resolution(s): {joined}. Required next lookup(s): {steps}."
    )
    return _human_nudge(content, _SRC_ENTITY, nemotron_entity_pre_nudged=True)


def _entity_after(state):
    if state.get("nemotron_entity_guard_fired"):
        return None
    messages = list(state.get("messages") or [])
    if _repair_risk(messages) or not messages:
        return None
    last = messages[-1]
    if not isinstance(last, AIMessage) or not _is_final(last):
        return None
    user_text = _latest_user_text(messages[:-1])
    final_text = _text_of(last)
    missing = _missing_resolutions(messages[:-1], user_text, final_text)
    if not missing:
        return None
    steps = _resolution_steps(missing)
    joined = ", ".join(missing)
    content = (
        "Your final answer is using or mixing opaque entity IDs before "
        "resolving them to user-facing details. Keep each branch bound to "
        f"the ID that produced it. Resolve these before answering: {joined}. "
        "If a matching name/details lookup tool is available, call it now, "
        f"then answer from that result. Required next lookup(s): {steps}. "
        "Do not reuse a name or details from a different entity or question "
        "branch."
    )
    return _human_nudge(
        content,
        _SRC_ENTITY,
        jump_to="model",
        nemotron_entity_guard_fired=True,
    )


def _literal_nudge(calls, final_lower):
    missing = [item for item in _mutation_literals(calls) if item.lower() not in final_lower]
    if not missing:
        return None
    joined = ", ".join(missing)
    content = (
        "Your final answer omitted exact literal value(s) from the completed "
        f"tool action: {joined}. Answer again and include each literal exactly, "
        "along with the concrete result."
    )
    return _human_nudge(
        content,
        _SRC_FINAL,
        jump_to="model",
        nemotron_final_guard_fired=True,
    )


def _mutation_result_nudge(found, final_text):
    if found is None:
        return None
    call, value = found
    summary = _clip(value)
    if not summary or _VAGUE.fullmatch(final_text.strip()) is None:
        return None
    tool = call.get("name", "tool")
    content = (
        "Your final answer should communicate the concrete outcome of the "
        "completed state-changing tool call. Latest mutation "
        f"tool: {tool}. Observed result: {summary}. Answer again from that "
        "result, including what changed and any important status, amount, "
        "date/time, identifier, or remaining caveat present in the tool result."
    )
    return _human_nudge(
        content,
        _SRC_FINAL,
        jump_to="model",
        nemotron_final_guard_fired=True,
    )


def _final_update(state):
    messages = list(state.get("messages") or [])
    if state.get("nemotron_final_guard_fired") or _repair_risk(messages) or not messages:
        return None
    last = messages[-1]
    if not isinstance(last, AIMessage) or not _is_final(last):
        return None
    final_text = _text_of(last)
    user_text = _latest_user_text(messages[:-1])
    if _exact_reply_met(user_text, final_text):
        return None
    literal = _literal_nudge(_flat_tool_calls(messages[:-1]), final_text.lower())
    if literal is not None:
        return literal
    return _mutation_result_nudge(_latest_mutation(messages[:-1]), final_text)


class NemotronToolCallShim(AgentMiddleware):
    """把文件系统工具的 path 参数和空结果收拾成模型读得懂的形状。"""

    name = "NemotronToolCallShim"

    @staticmethod
    def _adjust(request):
        call = request.tool_call
        name = call.get("name")
        if name not in _PATH_TOOLS:
            return request
        args = dict(call.get("args") or {})
        changed = False
        if "path" in args and "file_path" not in args:
            args["file_path"] = args.pop("path")
            changed = True
        if name == "read_file" and "limit" not in args:
            args["limit"] = _PAGE
            changed = True
        if not changed:
            return request
        return request.override(tool_call={**call, "args": args})

    @staticmethod
    def _fill(result):
        if isinstance(result, ToolMessage) and _content_blank(result.content):
            return result.model_copy(update={"content": _EMPTY_RESULT})
        return result

    def wrap_tool_call(self, request, handler):
        revised = self._adjust(request)
        return self._fill(handler(revised))

    async def awrap_tool_call(self, request, handler):
        revised = self._adjust(request)
        return self._fill(await handler(revised))


class ReadFileContinuationNoticeMiddleware(AgentMiddleware):
    """读满一页时告诉模型文件可能还没结束。"""

    name = "ReadFileContinuationNoticeMiddleware"

    @staticmethod
    def _annotate(request, result):
        if not isinstance(result, ToolMessage) or request.tool_call.get("name") != "read_file":
            return result
        content = result.text
        if not content or content.startswith("Error"):
            return result
        args = request.tool_call.get("args", {}) or {}
        offset = _page_number(args.get("offset"), 0)
        limit = _page_number(args.get("limit"), _PAGE)
        scanned = content.split("\n", _HEADER_SPLIT)[:_HEADER_SPLIT]
        header = next((item for item in (_HEADER.match(row) for row in scanned) if item), None)
        if header is not None:
            count = int(header.group(2)) - int(header.group(1)) + 1
        else:
            count = sum(_NUMBERED.match(row) is not None for row in content.split("\n"))
        if count < limit:
            return result
        notice = (
            f"\n\n[read_file returned {limit} lines starting at offset {offset}, "
            f"the per-read limit. The file likely continues past this window. "
            f"To read further, call read_file again with offset={offset + limit}. "
            f"Do not assume you have seen the end of the file.]"
        )
        return result.model_copy(update={"content": content + notice})

    def wrap_tool_call(self, request, handler):
        return self._annotate(request, handler(request))

    async def awrap_tool_call(self, request, handler):
        return self._annotate(request, await handler(request))


class ModelRateLimitRetryMiddleware(AgentMiddleware):
    """模型调用碰上 429 时按固定间隔再试。"""

    name = "ModelRateLimitRetryMiddleware"

    def __init__(self, retry_delays: tuple[float, ...] = _RATE_PAUSES) -> None:
        self._retry_delays = retry_delays

    def wrap_model_call(self, request, handler):
        for pause in (*self._retry_delays, None):
            try:
                return handler(request)
            except Exception as exc:
                if pause is None or not _rate_limited(exc):
                    raise
                time.sleep(pause)
        raise RuntimeError(_UNREACHABLE)

    async def awrap_model_call(self, request, handler):
        for pause in (*self._retry_delays, None):
            try:
                return await handler(request)
            except Exception as exc:
                if pause is None or not _rate_limited(exc):
                    raise
                await asyncio.sleep(pause)
        raise RuntimeError(_UNREACHABLE)


class ChatNVIDIAMessageCompatibilityMiddleware(AgentMiddleware):
    """把标准 tool call 字段抄进 ChatNVIDIA 要读的 additional_kwargs。"""

    name = "ChatNVIDIAMessageCompatibilityMiddleware"

    @staticmethod
    def _repair_one(message):
        if isinstance(message, AIMessage):
            if not message.tool_calls or "tool_calls" in message.additional_kwargs:
                return message
            extra = dict(message.additional_kwargs)
            extra["tool_calls"] = _openai_tool_calls(message.tool_calls)
            return message.model_copy(update={"additional_kwargs": extra})
        if isinstance(message, ToolMessage) and message.name and "name" not in message.additional_kwargs:
            extra = dict(message.additional_kwargs)
            extra["name"] = message.name
            return message.model_copy(update={"additional_kwargs": extra})
        return message

    @classmethod
    def _repair_all(cls, messages):
        rewritten = []
        changed = False
        for message in messages:
            if isinstance(message, (AIMessage, ToolMessage)):
                fixed = cls._repair_one(message)
                changed = changed or fixed is not message
                rewritten.append(fixed)
            else:
                rewritten.append(message)
        return rewritten if changed else messages

    @classmethod
    def _prepare(cls, request):
        original = list(request.messages or [])
        messages = cls._repair_all(original)
        if messages is original:
            return request
        return request.override(messages=messages)

    def wrap_model_call(self, request, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._prepare(request))


class NemotronReasoningTagCleanupMiddleware(AgentMiddleware):
    """把思考标签从助手正文里拿掉，另存到 reasoning_content。"""

    name = "NemotronReasoningTagCleanupMiddleware"

    def wrap_model_call(self, request, handler):
        return _map_model_result(handler(request), _without_think_tags)

    async def awrap_model_call(self, request, handler):
        return _map_model_result(await handler(request), _without_think_tags)


class NemotronTextToolCallParser(AgentMiddleware):
    """把写在正文里的工具调用还原成结构化 tool call。"""

    name = "NemotronTextToolCallParser"

    def wrap_model_call(self, request, handler):
        allowed = set(_names_on_request(request))

        def convert(message):
            return _repair_tool_message(message, allowed)

        return _map_model_result(handler(request), convert)

    async def awrap_model_call(self, request, handler):
        allowed = set(_names_on_request(request))

        def convert(message):
            return _repair_tool_message(message, allowed)

        return _map_model_result(await handler(request), convert)


class NemotronProgressBudgetMiddleware(AgentMiddleware):
    """模型回合、工具结果或重复调用超预算时直接收束。"""

    name = "NemotronProgressBudgetMiddleware"

    def __init__(
        self,
        *,
        max_model_calls: int = 16,
        max_tool_results: int = 48,
        max_repeated_tool_calls: int = 3,
    ) -> None:
        self.max_model_calls = max_model_calls
        self.max_tool_results = max_tool_results
        self.max_repeated_tool_calls = max_repeated_tool_calls

    def _reason(self, messages):
        turns = _ai_total(messages)
        if turns >= self.max_model_calls:
            return f"{turns} model turns"
        tools = _tool_total(messages)
        if tools >= self.max_tool_results:
            return f"{tools} tool results"
        repeats = _longest_repeat(messages)
        if repeats >= self.max_repeated_tool_calls:
            return f"{repeats} repeated identical tool calls"
        return None

    def _fallback(self, messages, reason):
        return AIMessage(
            content=_budget_paragraph(messages, reason),
            name=_SRC_BUDGET,
            response_metadata={"nemotron_progress_budget_reason": reason},
        )

    def wrap_model_call(self, request, handler):
        window = _since_last_user(_messages_for_request(request))
        reason = self._reason(window)
        if reason is not None:
            return self._fallback(window, reason)
        return handler(request)

    async def awrap_model_call(self, request, handler):
        window = _since_last_user(_messages_for_request(request))
        reason = self._reason(window)
        if reason is not None:
            return self._fallback(window, reason)
        return await handler(request)


class NemotronPolicyNudgeState(AgentState):
    """策略提醒是否已经发过。"""

    nemotron_transition_nudged: _HiddenFlag
    nemotron_action_nudged: _HiddenFlags
    nemotron_tool_chain_nudged: _HiddenFlag
    nemotron_domain_tool_nudged: _HiddenFlag


class NemotronPolicyNudgeMiddleware(AgentMiddleware):
    """在第一轮或工具返回后补上容易漏掉的策略提醒。"""

    name = "NemotronPolicyNudgeMiddleware"
    state_schema = NemotronPolicyNudgeState

    def wrap_model_call(self, request, handler):
        if _prefer_domain(request):
            names = [name for name in _names_on_request(request) if _is_domain(name)]
            hint = ", ".join(names[:_HINT_LIMIT])
            text = f"{_DOMAIN_LEAD} Relevant task tools include: {hint}."
            request = _append_note(request, text, _SRC_DOMAIN_PREF)
        if _prefer_filesystem(request):
            request = _append_note(request, _FS_NUDGE, _SRC_FS)
        return handler(request)

    async def awrap_model_call(self, request, handler):
        if _prefer_domain(request):
            names = [name for name in _names_on_request(request) if _is_domain(name)]
            hint = ", ".join(names[:_HINT_LIMIT])
            text = f"{_DOMAIN_LEAD} Relevant task tools include: {hint}."
            request = _append_note(request, text, _SRC_DOMAIN_PREF)
        if _prefer_filesystem(request):
            request = _append_note(request, _FS_NUDGE, _SRC_FS)
        return await handler(request)

    def before_model(self, state, runtime):
        del runtime
        return _merge_updates((
            _transition_update(state),
            _action_update(state),
            _chain_update(state),
            _domain_done_update(state),
        ))

    async def abefore_model(self, state, runtime):
        return self.before_model(state, runtime)


class FollowupDisciplineState(AgentState):
    """追问守卫是否已经打过一次回。"""

    nemotron_followup_guard_fired: _HiddenFlag


class FollowupDisciplineMiddleware(AgentMiddleware):
    """终答在重复追问时，把模型送回去重写一次。"""

    name = "FollowupDisciplineMiddleware"
    state_schema = FollowupDisciplineState

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state, runtime):
        del runtime
        return _followup_update(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state, runtime):
        return self.after_agent(state, runtime)


class EntityResolutionGuardState(AgentState):
    """实体分支提醒和终答回跳各记一次。"""

    nemotron_entity_pre_nudged: _HiddenFlag
    nemotron_entity_guard_fired: _HiddenFlag


class EntityResolutionGuardMiddleware(AgentMiddleware):
    """实体 id 还没解开，或终答混用了别的分支时，把模型送回去。"""

    name = "EntityResolutionGuardMiddleware"
    state_schema = EntityResolutionGuardState

    def before_model(self, state, runtime):
        del runtime
        return _entity_before(state)

    async def abefore_model(self, state, runtime):
        return self.before_model(state, runtime)

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state, runtime):
        del runtime
        return _entity_after(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state, runtime):
        return self.after_agent(state, runtime)


class FinalAnswerGuardState(AgentState):
    """终答守卫是否已经打过一次回。"""

    nemotron_final_guard_fired: _HiddenFlag


class FinalAnswerGuardMiddleware(AgentMiddleware):
    """终答漏了字面量，或只说完成了，就再要一次具体结果。"""

    name = "FinalAnswerGuardMiddleware"
    state_schema = FinalAnswerGuardState

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state, runtime):
        del runtime
        return _final_update(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state, runtime):
        return self.after_agent(state, runtime)


def _middleware_stack():
    retry = ToolRetryMiddleware(
        max_retries=1,
        tools=list(_FS_TOOLS),
        on_failure="continue",
        initial_delay=0.0,
        backoff_factor=1.0,
        max_delay=0.0,
        jitter=False,
    )
    stack = []
    stack.append(NemotronProgressBudgetMiddleware())
    stack.append(NemotronPolicyNudgeMiddleware())
    stack.append(NemotronToolCallShim())
    stack.append(ReadFileContinuationNoticeMiddleware())
    stack.append(retry)
    stack.append(ModelRateLimitRetryMiddleware())
    stack.append(ChatNVIDIAMessageCompatibilityMiddleware())
    stack.append(NemotronReasoningTagCleanupMiddleware())
    stack.append(NemotronTextToolCallParser())
    stack.append(FollowupDisciplineMiddleware())
    stack.append(EntityResolutionGuardMiddleware())
    stack.append(FinalAnswerGuardMiddleware())
    return stack


def _make_profile():
    return _RuntimeProfile(
        system_prompt_suffix=_PROMPT_SUFFIX,
        tool_description_overrides={"read_file": _READ_OVERRIDE},
        extra_middleware=_middleware_stack,
    )


def register() -> None:
    """把八把 Nemotron 3 Ultra 键登记成同一个画像对象。"""
    profile = _make_profile()
    for model_key in _MODEL_KEYS:
        _publish(model_key, profile)
