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
# 本文件中的模型可见文案原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""把过大的工具结果写进后端，并换成带首尾预览的工具消息。

调用方自己决定「算不算太大」。这里只做三件事：抽出文本、把全文写到给定前缀下面、
用一段固定说明换掉原来的文本。图片、音频这类非文本块留在新消息里。
本文件不写 ``lc_evicted_to``，也不做 token 换算。
"""

import uuid

from langchain_core import messages as lc_messages

from ...backends import utils as file_text

# 预览在加行号之前，先按字符把每一行切到这个长度。行号前缀不算在里面。
_LINE_CAP = 1000

TOO_LARGE_TOOL_MSG = """Tool result too large, the result of this tool call {tool_call_id} was saved in the filesystem at this path: {file_path}

You can read the result from the filesystem by using the read_file tool, but make sure to only read part of the result at a time.

You can do this by specifying an offset and limit in the read_file tool call. For example, to read the first 100 lines, you can use the read_file tool with offset=0 and limit=100.

Here is a preview showing the head and tail of the result (lines of the form `... [N lines truncated] ...` indicate omitted lines in the middle of the content):

{content_sample}
"""


def _numbered(rows: list[str], start_line: int) -> str:
    """给已经切好的行加上行号。头、尾必须分两次调用，宽度才不会被并到一起。"""
    return file_text.format_content_with_line_numbers(rows, start_line)


def _clip_rows(rows: list[str]) -> list[str]:
    return [row[:_LINE_CAP] for row in rows]


def _create_content_preview(content_str: str, *, head_lines: int = 5, tail_lines: int = 5) -> str:
    """按通用换行切开，短文本整段编号，长文本只留头尾并插一行省略提示。"""
    rows = content_str.splitlines()
    if len(rows) <= head_lines + tail_lines:
        return _numbered(_clip_rows(rows), 1)

    opening = _clip_rows(rows[:head_lines])
    # tail_lines 为 0 时，负零切片会得到整列。这里不要改成空列表。
    closing = _clip_rows(rows[-tail_lines:])
    omitted = len(rows) - head_lines - tail_lines
    marker = f"\n... [{omitted} lines truncated] ...\n"
    tail_start = len(rows) - tail_lines + 1
    return _numbered(opening, 1) + marker + _numbered(closing, tail_start)


def _extract_text_from_message(incoming: lc_messages.BaseMessage) -> str:
    """只拼接 type 为 text 的块。缺 text 键就让 KeyError 冒出去。"""
    pieces = [block["text"] for block in incoming.content_blocks if block["type"] == "text"]
    return "\n".join(pieces)


def _build_evicted_content(message: lc_messages.ToolMessage, replacement_text: str):
    """字符串直接换掉。列表则把非文本块原对象留在新文本块后面。"""
    if isinstance(message.content, str):
        return replacement_text

    kept = [block for block in message.content_blocks if block["type"] != "text"]
    if not kept:
        return replacement_text
    lead = {"type": "text", "text": replacement_text}
    return [lead, *kept]


def _build_evicted_tool_message(message: lc_messages.ToolMessage, evicted_content):
    """走 ToolMessage 构造器，让列表里的块再被浅拷贝一层。"""
    fields = {
        "content": evicted_content,
        "tool_call_id": message.tool_call_id,
        "name": message.name,
        "id": message.id,
        "artifact": message.artifact,
        "status": message.status,
        "additional_kwargs": dict(message.additional_kwargs),
        "response_metadata": dict(message.response_metadata),
    }
    return lc_messages.ToolMessage(**fields)


def _storage_name(tool_call_id: str) -> str:
    """有 id 就净化后当文件名；空 id 用 unknown- 加 8 位十六进制。"""
    if tool_call_id:
        return file_text.sanitize_tool_call_id(tool_call_id)
    return "unknown-" + uuid.uuid4().hex[:8]


def _storage_path(directory: str, tool_call_id: str) -> str:
    """前缀原样拼接，不去掉多余的斜杠。"""
    return f"{directory}/{_storage_name(tool_call_id)}"


def _write_failed(outcome: object) -> bool:
    """None，或者 error 为真，都算没写成。不要用结果对象自己的真值。"""
    return outcome is None or outcome.error


def _replacement(message: lc_messages.ToolMessage, payload: str, path: str) -> lc_messages.ToolMessage:
    """用落盘路径和默认预览填那一段固定说明，再装回一条工具消息。"""
    sample = _create_content_preview(payload)
    notice = TOO_LARGE_TOOL_MSG.format(
        tool_call_id=message.tool_call_id,
        file_path=path,
        content_sample=sample,
    )
    body = _build_evicted_content(message, notice)
    return _build_evicted_tool_message(message, body)


def _offload_tool_message_content(message, payload, backend, directory):
    """同步写盘。写失败或写入函数返回 None 时，调用方应保留原消息。"""
    path = _storage_path(directory, message.tool_call_id)
    outcome = backend.write(path, payload)
    if _write_failed(outcome):
        return None
    return _replacement(message, payload, path)


async def _aoffload_tool_message_content(message, payload, backend, directory):
    """异步写盘。只调 awrite，不退回同步 write。"""
    path = _storage_path(directory, message.tool_call_id)
    outcome = await backend.awrite(path, payload)
    if _write_failed(outcome):
        return None
    return _replacement(message, payload, path)
