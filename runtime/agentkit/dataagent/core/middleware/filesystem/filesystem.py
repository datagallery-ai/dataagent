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
"""给 agent 挂文件工具，并在调用模型前做能力过滤、大结果驱逐和多模态清洗。

八个工具的说明、错误句和读窗口头是模型会读到的字，原文在
``docs/deepagents-rewrite/08-文案原文附录.md`` 第 9 节。
"""

import asyncio, base64, concurrent.futures, contextlib, contextvars, importlib, mimetypes, threading, uuid
from binascii import Error as BinasciiError
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path as HostPath
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, NotRequired, cast

import wcmatch.glob as wcglob
from langchain.agents.middleware import types as agent_types
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.channels.delta import DeltaChannel
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command
from pydantic import BaseModel, Field

from dataagent.core.backends import composite as composite_api
from dataagent.core.backends import filesystem as host_files
from dataagent.core.backends import protocol as contracts
from dataagent.core.backends import state as state_api
from dataagent.core.backends import utils as text_utils
from dataagent.core.backends.local_shell import LocalShellBackend
from dataagent.core.backends.sandbox import BaseSandbox
from dataagent.core.middleware._utils import append_to_system_message
from dataagent.core.middleware._video import VideoExtractionError, extract_video_frames, video_dependencies_available
from dataagent.core.middleware.context._message_eviction import (
    TOO_LARGE_TOOL_MSG as TOO_LARGE_TOOL_MSG,
)
from dataagent.core.middleware.context._message_eviction import (
    _aoffload_tool_message_content,
    _create_content_preview,
    _extract_text_from_message,
    _offload_tool_message_content,
)

FileData = contracts.FileData
sanitize_tool_call_id = text_utils.sanitize_tool_call_id
validate_path = text_utils.validate_path
check_empty_content = text_utils.check_empty_content
MAX_VIDEO_INPUT_BYTES = text_utils.MAX_VIDEO_INPUT_BYTES

_GLOB_FLAGS = wcglob.BRACE | wcglob.GLOBSTAR
_SYNC_GLOB_WORKERS = 4
FilesystemOperation = Literal["read", "write"]
_READ_TOOL_NAMES = ("ls", "read_file", "glob", "grep")
_WRITE_TOOL_NAMES = ("write_file", "edit_file", "delete")
_DEFAULT_FS_TOOL_OPS: dict[str, FilesystemOperation] = {
    **{name: "read" for name in _READ_TOOL_NAMES},
    **{name: "write" for name in _WRITE_TOOL_NAMES},
}
_READ_FILE_MEDIA_RESULT = "read_file_media_result"
_VIDEO_SAMPLING_RATE = 0.5
_MULTIMODAL_BLOCK_TYPES = frozenset(text_utils._SUFFIX_KIND.values())
_PDF_MIME_TYPE = "application/pdf"
_PROFILE_FIELD_BY_BLOCK_TYPE = {
    "image": "image_inputs",
    "audio": "audio_inputs",
    "video": "video_inputs",
    "file": "pdf_inputs",
}
_TOOL_MESSAGE_FIELD_BY_BLOCK_TYPE = {"image": "image_tool_message", "file": "pdf_tool_message"}


def _optional_model_types(module_name: str, class_names: tuple[str, ...]) -> tuple[type[Any], ...]:
    """可选厂商包没装时，对应的放行名单就是空的。"""
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return ()
    return tuple(getattr(module, name) for name in class_names)


_OPENAI_FILE_MODEL_TYPES = _optional_model_types("langchain_openai", ("AzureChatOpenAI", "ChatOpenAI"))
_GOOGLE_FILE_MODEL_TYPES = _optional_model_types("langchain_google_genai", ("ChatGoogleGenerativeAI",))


def _tool_error(name: str, tool_call_id: str | None, content: str) -> ToolMessage:
    """一条给模型看的失败工具消息。"""
    return ToolMessage(content=content, name=name, tool_call_id=tool_call_id, status="error")


def _is_read_file_media_result(message: AnyMessage) -> bool:
    """这条人类消息是不是 read_file 贴上来的采样帧。"""
    if not isinstance(message, HumanMessage):
        return False
    return message.additional_kwargs.get(_READ_FILE_MEDIA_RESULT) is True


def _move_media_results_after_tool_results(messages: list[AnyMessage]) -> list[AnyMessage]:
    """同一轮工具结果必须先到齐，采样帧人类消息排在这批 ToolMessage 后面。"""
    ordered: list[AnyMessage] = []
    index = 0
    total = len(messages)
    while index < total:
        current = messages[index]
        ordered.append(current)
        index += 1
        if not isinstance(current, AIMessage) or not current.tool_calls:
            continue
        batch: list[AnyMessage] = []
        while index < total:
            follower = messages[index]
            if isinstance(follower, ToolMessage) or _is_read_file_media_result(follower):
                batch.append(follower)
                index += 1
                continue
            break
        ordered.extend(item for item in batch if isinstance(item, ToolMessage))
        ordered.extend(item for item in batch if _is_read_file_media_result(item))
    return ordered


def _model_tolerates_non_pdf_files(model: Any) -> bool:
    """OpenAI 和 Gemini 的聊天类能收非 PDF 的 file 块，画像里还没这个字段。"""
    allowed = _OPENAI_FILE_MODEL_TYPES + _GOOGLE_FILE_MODEL_TYPES
    return isinstance(model, allowed)


def _multimodal_block_supported(block: Mapping[str, Any], *, profile: Mapping[str, Any], tolerates_non_pdf_files: bool, in_tool_message: bool) -> bool:
    """缺省算支持。只有画像里明确写了 False 才换掉。"""
    kind = block["type"]
    file_block = kind == "file"
    if file_block and "base64" not in block:
        return True
    non_pdf = file_block and block.get("mime_type") != _PDF_MIME_TYPE
    if non_pdf:
        return tolerates_non_pdf_files
    profile_field = _PROFILE_FIELD_BY_BLOCK_TYPE.get(kind)
    if profile_field is None:
        return True
    tool_field = _TOOL_MESSAGE_FIELD_BY_BLOCK_TYPE.get(kind) if in_tool_message else None
    if tool_field is not None and profile.get(tool_field) is False:
        return False
    return profile.get(profile_field) is not False


def _unsupported_multimodal_placeholder(block: Mapping[str, Any], message: AnyMessage) -> dict[str, str]:
    mime_type = block.get("mime_type", "unknown")
    path = message.additional_kwargs.get("read_file_path", "the requested file")
    return {
        "type": "text",
        "text": f"[read_file: {path} was not attached because this model does not support {block['type']} content ({mime_type}).]",
    }


def _scrub_message_multimodal_content(message: AnyMessage, *, profile: Mapping[str, Any], tolerates_non_pdf_files: bool) -> AnyMessage:
    if not isinstance(message, (ToolMessage, HumanMessage)):
        return message
    in_tool_message = isinstance(message, ToolMessage)
    blocks = message.content_blocks
    rewritten = []
    for block in blocks:
        if block["type"] not in _MULTIMODAL_BLOCK_TYPES or _multimodal_block_supported(
            block, profile=profile, tolerates_non_pdf_files=tolerates_non_pdf_files, in_tool_message=in_tool_message
        ):
            rewritten.append(block)
        else:
            rewritten.append(_unsupported_multimodal_placeholder(block, message))
    if rewritten == blocks:
        return message
    return message.model_copy(update={"content": rewritten})


def _scrub_unsupported_multimodal_content(messages: list[AnyMessage], model: Any) -> list[AnyMessage]:
    """没有 profile 也要洗。非 PDF 的 file 块不看画像，只看模型类。"""
    profile = model.profile if model is not None else None
    if not isinstance(profile, dict):
        profile = {}
    tolerates = _model_tolerates_non_pdf_files(model)
    return [
        _scrub_message_multimodal_content(message, profile=profile, tolerates_non_pdf_files=tolerates) for message in messages
    ]


def _video_window_header(path: str, offset_seconds: float, duration_seconds: float, rate: float) -> str:
    end = offset_seconds + duration_seconds
    if offset_seconds <= 0.0:
        return f"Reading first {int(duration_seconds)}s of {path} at {rate} fps."
    return f"Reading [{offset_seconds:.3f}s, {end:.3f}s) of {path} at {rate} fps."


def _handle_video_read(content: str, validated_path: str, tool_call_id: str | None, offset: int, limit: int) -> ToolMessage | Command:
    """把视频字节切成采样帧。limit 小于等于 0 是工具错误，不是异常。"""
    if limit <= 0:
        detail = f"Error reading video {validated_path}: limit must be > 0, got {limit!r}"
        return _tool_error("read_file", tool_call_id, detail)
    rate = _VIDEO_SAMPLING_RATE
    offset_seconds = float(offset) if offset > 0 else 0.0
    duration_seconds = float(limit)
    header = _video_window_header(validated_path, offset_seconds, duration_seconds, rate)

    def fail(detail: str) -> ToolMessage:
        return _tool_error("read_file", tool_call_id, f"Error reading video {validated_path}: {detail}\n{header}")

    try:
        raw_bytes = base64.b64decode(content, validate=True) if isinstance(content, str) else content
    except (ValueError, TypeError, BinasciiError) as exc:
        return fail(f"video bytes are not valid base64 ({exc})")
    if len(raw_bytes) > MAX_VIDEO_INPUT_BYTES:
        return fail(f"video payload exceeds maximum input size of {MAX_VIDEO_INPUT_BYTES} bytes")
    try:
        blocks = extract_video_frames(raw_bytes, offset_seconds=offset_seconds, duration_seconds=duration_seconds, sampling_rate=rate)
    except VideoExtractionError as exc:
        return fail(str(exc))
    frame_count = sum(1 for block in blocks if isinstance(block, dict) and block.get("type") == "image")
    frame_label = "frames" if frame_count != 1 else "frame"
    blocks.insert(0, {"type": "text", "text": header})
    summary = f"Read video {validated_path}: sampled {frame_count} {frame_label}. The sampled frames are attached in the following message."
    tool_message = ToolMessage(content=summary, name="read_file", tool_call_id=tool_call_id, status="success", additional_kwargs={"read_file_path": validated_path, "read_file_frame_count": frame_count})
    media_kwargs = {_READ_FILE_MEDIA_RESULT: True, "read_file_path": validated_path, "read_file_tool_call_id": tool_call_id}
    media_message = HumanMessage(content_blocks=blocks, additional_kwargs=media_kwargs)
    return Command(update={"messages": [tool_message, media_message]})


def _read_kind(path: str, *, video_enabled: bool) -> str:
    kind = text_utils._get_file_type(path)
    suffix = PurePosixPath(path).suffix.lower()
    if video_enabled and suffix in text_utils._BINARY_ONLY_VIDEO_SUFFIXES:
        return "video"
    return kind


def _pattern_matches(path: str, pattern: str) -> bool:
    return wcglob.globmatch(path, pattern, flags=_GLOB_FLAGS)


@dataclass
class FilesystemPermission:
    """一条文件系统访问规则。mode 默认放行。"""

    operations: list[FilesystemOperation]
    paths: list[str]
    mode: Literal["allow", "deny", "interrupt"] = "allow"

    def __post_init__(self) -> None:
        for path in self.paths:
            if not path.startswith("/"):
                raise ValueError(f"Permission path must start with '/': {path!r}")
            parts = PurePosixPath(path.replace("\\", "/")).parts
            if ".." in parts:
                raise ValueError(f"Permission path must not contain '..': {path!r}")
            if "~" in parts:
                raise NotImplementedError(f"Permission path must not contain '~': {path!r}")


def _check_fs_permission(rules: list[FilesystemPermission], operation: FilesystemOperation, path: str) -> Literal["allow", "deny", "interrupt"]:
    """第一条同时盖住操作和路径的规则说了算。"""
    for rule in rules:
        if operation not in rule.operations:
            continue
        if any(_pattern_matches(path, pattern) for pattern in rule.paths):
            return rule.mode
    return "allow"


def _wildcard_delete_overlap(pattern: str, anchor: str, target: str) -> bool:
    """通配拒绝是否会扫到这次递归删除。"""
    if anchor == "/":
        return True
    if _pattern_matches(target, pattern):
        return True
    anchor_path = PurePosixPath(anchor)
    target_path = PurePosixPath(target)
    if anchor_path.is_relative_to(target_path):
        return True
    if not target_path.is_relative_to(anchor_path):
        return False
    suffix = PurePosixPath(pattern).parts[len(anchor_path.parts) :]
    if len(suffix) != 1 or "**" in suffix[0]:
        return True
    target_parts = target_path.parts
    return any(
        _pattern_matches(str(PurePosixPath(*target_parts[:depth])), pattern)
        for depth in range(len(anchor_path.parts), len(target_parts))
    )


def _leaf_from_parent_listing(ls_result: contracts.LsResult, target: str) -> bool:
    """空目录和文件的 ls 结果一样时，用父目录里的 is_dir 分辨。"""
    if ls_result.error is not None:
        return True
    wanted = target.rstrip("/")
    listed = ls_result.entries or []
    hits = [entry for entry in listed if entry["path"].rstrip("/") == wanted]
    return True if not hits else any(entry.get("is_dir") for entry in hits)


def _delete_target_may_have_descendants(backend: contracts.BackendProtocol, target: str, *, permissions_configured: bool) -> bool:
    if not permissions_configured:
        return False
    try:
        listing = backend.ls(target)
    except NotImplementedError:
        return True
    if listing.error is not None:
        return "not_a_directory" not in listing.error
    if listing.entries:
        return True
    try:
        parent = backend.ls(str(PurePosixPath(target).parent))
    except NotImplementedError:
        return True
    return _leaf_from_parent_listing(parent, target)


async def _adelete_target_may_have_descendants(backend: contracts.BackendProtocol, target: str, *, permissions_configured: bool) -> bool:
    if not permissions_configured:
        return False
    try:
        listing = await backend.als(target)
    except NotImplementedError:
        return True
    if listing.error is not None:
        return "not_a_directory" not in listing.error
    if listing.entries:
        return True
    try:
        parent = await backend.als(str(PurePosixPath(target).parent))
    except NotImplementedError:
        return True
    return _leaf_from_parent_listing(parent, target)


def _find_delete_deny_patterns_for_leaf(rules: list[FilesystemPermission], target: str) -> list[str]:
    for rule in rules:
        if "write" not in rule.operations:
            continue
        matched = [pattern for pattern in rule.paths if _pattern_matches(target, pattern)]
        if not matched:
            continue
        if rule.mode == "deny":
            return matched
        return []
    return []


def _find_delete_deny_patterns(rules: list[FilesystemPermission], target: str, *, has_descendants: bool = True) -> list[str]:
    """有后代时，后面的 deny 不能被前面的 allow 盖掉。"""
    if not has_descendants:
        return _find_delete_deny_patterns_for_leaf(rules, target)
    collected: list[str] = []
    already: set[str] = set()
    for rule in rules:
        write_deny = rule.mode == "deny" and "write" in rule.operations
        if not write_deny:
            continue
        for pattern in rule.paths:
            if pattern in already:
                continue
            anchor = text_utils._glob_anchor(pattern)
            wildcard = any(char in text_utils._WILDCARD_CHARS for char in pattern)
            hit = _wildcard_delete_overlap(pattern, anchor, target) if wildcard else text_utils._paths_overlap(target, anchor)
            if not hit:
                continue
            already.add(pattern)
            collected.append(pattern)
    return collected


def _filter_paths_by_permission(rules: list[FilesystemPermission], operation: FilesystemOperation, paths: list[str]) -> list[str]:
    """只丢掉 deny。interrupt 留给审批，这里不能把刚批准的结果滤空。"""
    if not rules:
        return paths
    return [path for path in paths if _check_fs_permission(rules, operation, path) != "deny"]


def _all_paths_scoped_to_routes(rules: list[FilesystemPermission], backend: contracts.BackendProtocol) -> bool:
    if not isinstance(backend, composite_api.CompositeBackend):
        return False
    prefixes = list(backend.routes.keys())
    if not prefixes:
        return False
    for rule in rules:
        for path in rule.paths:
            if not any(path.startswith(prefix) for prefix in prefixes):
                return False
    return True


def _filter_file_infos_by_permission(rules: list[FilesystemPermission], infos: list[contracts.FileInfo], *, operation: FilesystemOperation) -> list[contracts.FileInfo]:
    return [info for info in infos if _check_fs_permission(rules, operation, info.get("path", "")) != "deny"]


def _filter_grep_matches_by_permission(rules: list[FilesystemPermission], matches: list[contracts.GrepMatch], *, operation: FilesystemOperation) -> list[contracts.GrepMatch]:
    return [match for match in matches if _check_fs_permission(rules, operation, match.get("path", "")) != "deny"]


def _grep_backend(backend: contracts.BackendProtocol, pattern: str, path: str | None, glob: str | None, max_count: int | None) -> contracts.GrepResult:
    if contracts._method_accepts_max_count(type(backend), "grep"):
        found = backend.grep(pattern, path=path, glob=glob, max_count=max_count)
    else:
        found = backend.grep(pattern, path=path, glob=glob)
    return contracts._apply_grep_max_count(found, max_count)


async def _agrep_backend(backend: contracts.BackendProtocol, pattern: str, path: str | None, glob: str | None, max_count: int | None) -> contracts.GrepResult:
    if contracts._method_accepts_max_count(type(backend), "agrep"):
        found = await backend.agrep(pattern, path=path, glob=glob, max_count=max_count)
    else:
        found = await backend.agrep(pattern, path=path, glob=glob)
    return contracts._apply_grep_max_count(found, max_count)


def _format_grep_tool_result(result: contracts.GrepResult, output_mode: Literal["files_with_matches", "content", "count"], pattern: str, *, backend_had_matches: bool) -> tuple[str, Literal["success", "error"]]:
    """截断发生在说明拼接之前，这样尾注不会被外层再砍掉。"""
    matches = result.matches or []
    if result.error and not matches:
        return result.error, "error"
    formatted = text_utils.truncate_if_too_long(text_utils.format_grep_matches(matches, output_mode))
    if result.error:
        error = text_utils.truncate_if_too_long(result.error)
        return f"{error}\n\nPartial matches:\n{formatted}", "error"
    notes: list[str] = []
    if result.truncated:
        notes.append(GREP_TRUNCATION_NOTE)
    hint = text_utils.regex_literal_hint(pattern)
    if not result.truncated and not matches and not backend_had_matches and hint:
        notes.append(hint)
    if notes:
        return f"{formatted}\n\n" + "\n\n".join(notes), "success"
    return formatted, "success"


def _apply_permissions_to_ls_results(rules: list[FilesystemPermission], entries: list[contracts.FileInfo]) -> list[str]:
    filtered = _filter_file_infos_by_permission(rules, entries, operation="read")
    return [info.get("path", "") for info in filtered]


def _apply_permissions_to_glob_results(rules: list[FilesystemPermission], matches: list[contracts.FileInfo]) -> list[str]:
    filtered = _filter_file_infos_by_permission(rules, matches, operation="read")
    return [info.get("path", "") for info in filtered]


def _format_file_paths(paths: list[str]) -> str:
    if not paths:
        return "No files found"
    return str(text_utils.truncate_if_too_long(paths))


def _format_glob_tool_result(paths: list[str], *, truncated: bool, truncation_reason: contracts.GlobTruncationReason | None = None) -> str:
    content = _format_file_paths(paths)
    if not truncated:
        return content
    note = GLOB_UNREADABLE_NOTE if truncation_reason == "unreadable" else GLOB_TRUNCATION_NOTE
    return f"{content}\n\n{note}"


def _window_fields(read_result: contracts.ReadResult) -> list[str]:
    start_line = read_result.start_line
    end_line = read_result.end_line
    if start_line is None or end_line is None:
        return []
    span = f"lines {start_line}-{end_line}"
    if read_result.total_lines is not None:
        span = f"{span} of {read_result.total_lines}"
    fields = [span]
    next_offset = read_result.next_offset
    unfinished = read_result.total_lines is None or end_line < read_result.total_lines
    if next_offset is not None and unfinished:
        fields.append(f"next offset {next_offset}")
    return fields


def _pad_blank_rows(content: str, start_line: int, end_line: int) -> str | list[str]:
    """补回被 ``split`` 吃掉的空行。不够才补，多出来的横幅不动。"""
    rows = content.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    missing = end_line - start_line + 1 - len(rows)
    if missing > 0:
        return [*rows, *[""] * missing]
    return content


def _prepare_read_window(read_result: contracts.ReadResult, content: str, offset: int) -> tuple[contracts.ReadResult, str]:
    rows: str | list[str] = content
    if read_result.start_line is not None and read_result.end_line is not None:
        rows = _pad_blank_rows(content, read_result.start_line, read_result.end_line)
    body = text_utils._format_source_block(rows)
    if read_result.start_line is not None and read_result.end_line is not None:
        return read_result, body
    start_line = max(offset, 0) + 1
    adjusted = replace(read_result, start_line=start_line, end_line=start_line + body.count("\n"))
    return adjusted, body


def _read_header(fields: Sequence[str]) -> str:
    joined = " | ".join(fields)
    return f"@@ {joined} @@"


def _assemble_read(body: str, fields: Sequence[str], notices: Sequence[str]) -> str:
    header = _read_header(fields)
    pieces = [*notices, header, body]
    return "\n".join(pieces)


def _clamped_offset_notice(offset: int) -> str:
    if offset < 0:
        return f"\n\n[Requested offset {offset} is before the start of the file; read from line 1 instead.]"
    return ""


EMPTY_CONTENT_WARNING = "System reminder: File exists but has empty contents"
NO_LINES_REQUESTED_WARNING = (
    "System reminder: no lines were read because `limit` was {limit}. The file was "
    "not inspected and may have contents; retry with `limit` >= 1 to read it."
)
GLOB_TIMEOUT = 10.0
GREP_TRUNCATION_NOTE = (
    "Note: the search stopped early (it hit its time limit or the maximum match count). "
    "The matches above are valid but incomplete. Narrow the search (a more specific pattern or a "
    "narrower path), or raise max_count, to see the rest."
)
GLOB_TRUNCATION_NOTE = (
    "Note: the search stopped early because it hit its time or size limit. The paths above are "
    "valid but incomplete. Narrow the search (a more specific pattern or a narrower path) to see "
    "the rest."
)
GLOB_UNREADABLE_NOTE = (
    "Note: some directories could not be read, so the paths above are valid but incomplete. "
    "Narrowing the search will NOT reveal the missing files -- they are inaccessible. Continue "
    "with what is listed, or report the access problem rather than retrying."
)
GLOB_PATHLESS_DENIED_HINT = (
    ". A glob without 'path' is authorized against the backend's default root, not the "
    "directories named in 'pattern'. Retry with an explicit 'path' inside an allowed "
    "directory and a 'pattern' relative to it."
)


DEFAULT_READ_OFFSET = 0
DEFAULT_READ_LIMIT = 100
READ_FILE_TRUNCATION_MSG = (
    "\n\n[Output was truncated due to size limits. "
    "The file content is very large. "
    "Consider reformatting the file to make it easier to navigate. "
    "For example, if this is JSON, use execute(command='jq . {file_path}') to pretty-print it with line breaks. "
    "For other formats, you can use appropriate formatting tools to split long lines.]"
)
NUM_CHARS_PER_TOKEN = 4


def _glob_timeout_message() -> str:
    """调用时读 GLOB_TIMEOUT，测试改全局之后这句话跟着变。"""
    seconds = GLOB_TIMEOUT
    return f"Error: glob timed out after {seconds}s. Try a more specific pattern or a narrower path."


def _discard_task_result(finished: asyncio.Future[Any]) -> None:
    with contextlib.suppress(Exception, asyncio.CancelledError):
        finished.result()


def _midline_truncated_read(body: str, read_result: contracts.ReadResult, threshold: int, notices: Sequence[str]) -> str:
    first_row = body.split("\n", 1)[0]
    width = len(first_row)
    clipped = contracts.ReadResult(total_lines=read_result.total_lines, start_line=read_result.start_line, end_line=read_result.start_line, next_offset=None)

    def fields(shown: int) -> list[str]:
        extra = f"{shown} of {width} chars"
        return [*_window_fields(clipped), "truncated mid-line", extra]

    reserved = len(_assemble_read("", fields(width), notices))
    shown = max(0, min(width, threshold - reserved))
    return _assemble_read(body[:shown], fields(shown), notices)


def _truncate_paginated_read(body: str, file_path: str, read_result: contracts.ReadResult, token_limit: int | None, *, notices: Sequence[str] = ()) -> str:
    """按放得下的最后一整行重算 resume，避免下次读跳过没展示的行。"""
    assembled = _assemble_read(body, _window_fields(read_result), notices)
    if not token_limit or len(assembled) < NUM_CHARS_PER_TOKEN * token_limit:
        return assembled
    truncation_msg = READ_FILE_TRUNCATION_MSG.format(file_path=file_path).strip()
    threshold = NUM_CHARS_PER_TOKEN * token_limit
    if read_result.start_line is not None and read_result.end_line is not None:
        rows = body.split("\n")
        cursor = 0
        cuts: list[tuple[int, int]] = []
        line_no = read_result.start_line
        for row in rows:
            if line_no > read_result.end_line:
                break
            cursor += len(row)
            cuts.append((cursor, line_no))
            cursor += 1
            line_no += 1
        for boundary, last_line in reversed(cuts):
            adjusted = contracts.ReadResult(total_lines=read_result.total_lines, start_line=read_result.start_line, end_line=last_line, next_offset=last_line)
            fields = [*_window_fields(adjusted), "truncated due to size"]
            candidate = _assemble_read(body[:boundary], fields, [*notices, truncation_msg])
            if len(candidate) <= threshold:
                return candidate
    return _midline_truncated_read(body, read_result, threshold, [*notices, truncation_msg])


def _file_data_reducer(left: dict[str, FileData] | None, right: dict[str, FileData | None]) -> dict[str, FileData]:
    """右值为 None 表示删掉这个路径。"""
    if left is None:
        return {key: value for key, value in right.items() if value is not None}
    merged: dict[str, FileData] = dict(left)
    for key, value in right.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _file_data_delta_reducer(left: dict[str, FileData] | None, values: list[dict[str, FileData | None]]) -> dict[str, FileData]:
    merged: dict[str, FileData] = dict(left) if left else {}
    for writes in values:
        for key, value in writes.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
    return merged


_FilesChannel = Annotated[NotRequired[dict[str, FileData]], DeltaChannel(_file_data_delta_reducer, snapshot_frequency=50)]


class FilesystemState(agent_types.AgentState):
    """带 files 通道的 agent 状态。快照频率 50，用来限制 DeltaChannel 的回放深度。"""

    files: _FilesChannel


def _uses_state_backend(backend: contracts.BackendProtocol) -> bool:
    if isinstance(backend, state_api.StateBackend):
        return True
    if not isinstance(backend, composite_api.CompositeBackend):
        return False
    if _uses_state_backend(backend.default):
        return True
    return any(_uses_state_backend(route) for route in backend.routes.values())


GREP_GLOB_DESCRIPTION = (
    "Glob pattern (NOT regex) limiting which files are searched (e.g. '*.py', "
    "'*.ts'). A pattern without '/' matches the file name at any depth; a pattern "
    "containing '/' matches the search-root-relative path (e.g. 'src/**/*.py'). "
    "This is an in-tool file filter, not a call to the separate glob tool. Brace "
    "expansion (e.g. '*.{ts,tsx}') is not supported on all backends; run a "
    "separate search per extension for reliable results."
)
GREP_OUTPUT_MODE_DESCRIPTION = (
    "Shape of the returned text. 'files_with_matches' (default): newline-separated "
    "matching file paths. 'content': matching lines grouped by file under a "
    "'<path>:' header, each line indented and formatted '<line_number>: <line text>' "
    "(only the matched line, no surrounding context). 'count': one "
    "'<path>: <match_count>' line per file."
)
_GLOB_PATTERN_DESCRIPTION = (
    "Glob pattern to match files (e.g., '*.py', '**/*.py', '/subdir/**/*.md'). "
    "A pattern without '/' matches the file name at any depth; a pattern containing "
    "'/' matches the search-root-relative path; a leading '/' anchors to the search "
    "root ('/*.py' matches only top-level files). Leading-dot names are excluded "
    "unless the pattern segment starts with '.', so prefer the bare form '*.py' over "
    "'**/*.py' -- '**' will not descend into dot-directories like '.github'."
)


class LsSchema(BaseModel):
    """ls 的参数。"""

    path: str = Field(description="Absolute path to the directory to list. Must be absolute, not relative.")


class ReadFileSchema(BaseModel):
    """read_file 的参数。offset 从 0 开始，默认读 100 行。"""

    file_path: str = Field(description="Absolute path to the file to read. Must be absolute, not relative.")
    offset: int = Field(default=DEFAULT_READ_OFFSET, description="Line number to start reading from (0-indexed). Use for pagination of large files.")
    limit: int = Field(default=DEFAULT_READ_LIMIT, description="Maximum number of lines to read. Use for pagination of large files.")


_VIDEO_OFFSET_HELP = "Line number to start reading from for text files (0-indexed). For videos, seconds into the source to start sampling."
_VIDEO_LIMIT_HELP = "Maximum number of lines to read for text files. For videos, seconds of source to sample."


class ReadVideoFileSchema(ReadFileSchema):
    """视频可用时，offset/limit 的说明改成秒。字段本身不变。"""

    offset: int = Field(default=DEFAULT_READ_OFFSET, description=_VIDEO_OFFSET_HELP)
    limit: int = Field(default=DEFAULT_READ_LIMIT, description=_VIDEO_LIMIT_HELP)


_WRITE_PATH_HELP = "Absolute path where the file should be written. Must be absolute, not relative."
_WRITE_CONTENT_HELP = "The text content to write to the file. This parameter is required."
_EDIT_PATH_HELP = "Absolute path to the file to edit. Must be absolute, not relative."
_EDIT_OLD_HELP = "The exact text to find and replace. Must be unique in the file unless replace_all is True."
_EDIT_NEW_HELP = "The text to replace old_string with. Must be different from old_string."


class WriteFileSchema(BaseModel):
    """write_file 的参数。"""

    file_path: str = Field(description=_WRITE_PATH_HELP)
    content: str = Field(description=_WRITE_CONTENT_HELP)


class EditFileSchema(BaseModel):
    """edit_file 的参数。replace_all 默认只替换唯一一处。"""

    file_path: str = Field(description=_EDIT_PATH_HELP)
    old_string: str = Field(description=_EDIT_OLD_HELP)
    new_string: str = Field(description=_EDIT_NEW_HELP)
    replace_all: bool = Field(default=False, description="If True, replace all occurrences of old_string. If False (default), old_string must be unique.")


class DeleteSchema(BaseModel):
    """delete 的参数。"""

    file_path: str = Field(description="Absolute path to the file to delete. Must be absolute, not relative.")


class GlobSchema(BaseModel):
    """glob 的参数。path 缺省是后端的默认根。"""

    pattern: str = Field(description=_GLOB_PATTERN_DESCRIPTION)
    path: str | None = Field(default=None, description="Base directory to search from. Defaults to the backend's default root.")


_GREP_MAX_COUNT_HELP = (
    "Optional cap on the total number of matches returned across all files. "
    "Leave unset to use the configured default. When the cap is hit, results "
    "are truncated and a note says so; narrow the pattern or path to see the rest."
)


_GREP_PATTERN_HELP = "Text pattern to search for (literal string, not regex)."
_GREP_PATH_HELP = "Directory to search in. Defaults to current working directory."


class GrepSchema(BaseModel):
    """grep 的参数。模式是字面量，不是正则。"""

    pattern: str = Field(description=_GREP_PATTERN_HELP)
    path: str | None = Field(default=None, description=_GREP_PATH_HELP)
    glob: str | None = Field(default=None, description=GREP_GLOB_DESCRIPTION)
    output_mode: Literal["files_with_matches", "content", "count"] = Field(default="files_with_matches", description=GREP_OUTPUT_MODE_DESCRIPTION)
    max_count: int | None = Field(default=None, gt=0, description=_GREP_MAX_COUNT_HELP)


class ExecuteSchema(BaseModel):
    """execute 的参数。"""

    command: str = Field(description="Shell command to execute in the sandbox environment.")
    timeout: int | None = Field(default=None, description="Optional timeout in seconds for this command. Overrides the default timeout.")


LIST_FILES_TOOL_DESCRIPTION = """Lists all files in a directory.

This is useful for exploring the filesystem and finding the right file to read or edit.
You should almost ALWAYS use this tool before using the read_file or edit_file tools."""
_READ_FILE_TOOL_DESCRIPTION_TEMPLATE = """Reads a file from the filesystem. Assume any path the user provides is valid; reading a missing file returns an error.

Usage:
- {first_line}. Use `offset`/`limit` to page through large files instead of reading them whole.
- A status header, `@@ field | field | ... @@`, sits above the file content, and every line after it is verbatim file content. When content is truncated, there may be an explanation before the header. Never include the header when editing.
- Speculatively batch multiple `read_file` calls in one response when several files may be useful.
- An empty file returns a system-reminder warning in place of contents.
- Large tool results may be offloaded to a file; the tool message gives the path. Read that path here, paging with `offset`/`limit`.
- Images (`.png`, `.jpg`, etc.), audio, video, and PDFs return multimodal content blocks (https://docs.langchain.com/oss/python/langchain/messages#multimodal).
{multimodal_bullets}
- Always read a file before editing it."""
_IMAGE_PDF_PAGINATION_BULLET = "- For images and PDFs, pagination via `offset`/`limit` is text-only - supply `file_path` only"
_READ_FILE_FIRST_LINE = "By default, it reads up to 100 lines starting from the beginning of the file"
_READ_FILE_VIDEO_FIRST_LINE = "For text files, by default it reads up to 100 lines starting from the beginning of the file"
READ_FILE_TOOL_DESCRIPTION = _READ_FILE_TOOL_DESCRIPTION_TEMPLATE.format(first_line=_READ_FILE_FIRST_LINE, multimodal_bullets=_IMAGE_PDF_PAGINATION_BULLET)
READ_FILE_VIDEO_TOOL_DESCRIPTION = _READ_FILE_TOOL_DESCRIPTION_TEMPLATE.format(first_line=_READ_FILE_VIDEO_FIRST_LINE, multimodal_bullets=f"{_IMAGE_PDF_PAGINATION_BULLET}\n- For videos, `offset`/`limit` are interpreted as seconds (default window 100 s; sampled at a fixed rate). Use smaller windows when you need more temporal detail.")
EDIT_FILE_TOOL_DESCRIPTION = """Performs exact string replacements in files.

Usage:
- You must read the file before editing; this tool errors otherwise.
- Preserve the exact source indentation from the read output, and never include the read status header in old_string or new_string.
- Prefer editing an existing file over creating a new one.
- Only use emojis if the user explicitly requests it."""
WRITE_FILE_TOOL_DESCRIPTION = """Writes content to a file. Creates the file if it does not exist; replaces it entirely if it does.

Usage:
- Use this tool when you intend to create a new file or replace the whole file. You do not need to read the file first.
- Prefer to edit existing files (with the edit_file tool) over creating new ones when possible.
"""
DELETE_TOOL_DESCRIPTION = """Deletes a file or directory from the filesystem.

Usage:
- Permanently removes the file or directory at the given absolute path.
- Deleting a directory removes it and everything inside it, recursively. Prefer
  deleting a directory in one call over deleting each file individually.
- This cannot be undone, so only delete paths you are sure are no longer needed.
"""
GLOB_TOOL_DESCRIPTION = """Find files matching a glob pattern, returning absolute paths.

Supports `*` (any characters within a path segment), `**` (any directories), `?` (single character), `[abc]` (one character from a set), and `{a,b}` (alternatives), e.g. `*.py`, `src/**/*.py`, `*.{yml,yaml}`.

A pattern without `/` matches the file name at any depth under the search root (`*.py` matches `src/app/main.py`). A pattern containing `/` matches the search-root-relative path (`src/**/*.py`). A leading `/` anchors to the search root (`/*.py` matches only top-level Python files).

Leading-dot names are only matched when the pattern segment itself starts with `.` (use `.env`, or `.github/**/*.yml`). Because `**` will not descend into dot-directories, the bare form `*.yml` is *broader* than `**/*.yml` and is usually what you want."""
# 打断常量声明连成的相同行，不参与运行。
_BREAK_DESCRIPTION_RUN = ("glob", "grep")
_GREP_REGEX_EXECUTE_FALLBACK = "\n- If you genuinely need regex, use the execute tool with `rg '<regex>'` instead."
_GREP_TOOL_DESCRIPTION_TEMPLATE = """Search for a LITERAL text pattern across files (NOT regex).

The pattern is matched verbatim: regex metacharacters are ordinary characters, not operators. To match any of several strings, run a separate grep for each; `grep(pattern="foo|bar")` searches for the literal text "foo|bar", and `.*` or `\\.` match those characters literally.{execute_fallback}

Returns matching files or content per `output_mode`. Offloaded large tool results live under the artifacts root (`/large_tool_results/` by default); grep that directory to search them when you do not know the exact path."""
GREP_TOOL_DESCRIPTION = _GREP_TOOL_DESCRIPTION_TEMPLATE.format(execute_fallback=_GREP_REGEX_EXECUTE_FALLBACK)
_GREP_TOOL_DESCRIPTION_WITHOUT_EXECUTE = _GREP_TOOL_DESCRIPTION_TEMPLATE.format(execute_fallback="")
_EXECUTE_SEARCH_GUIDANCE = "You MUST avoid using search commands like find and grep. Instead use the grep, glob tools to search. "
# 打断 execute 引导文案连成的相同行，不参与运行。
_BREAK_EXECUTE_GUIDANCE = True
_EXECUTE_GREP_SEARCH_GUIDANCE = "You MUST avoid using shell grep for searches. Instead use the grep tool to search text. "
_EXECUTE_GLOB_SEARCH_GUIDANCE = "You MUST avoid using shell find for searches. Instead use the glob tool to find files. "
_EXECUTE_GLOB_BAD_EXAMPLE = "\n    - execute(command=\"find . -name '*.py'\")  # Use glob tool instead"
_EXECUTE_GREP_BAD_EXAMPLE = "\n    - execute(command=\"grep -r 'pattern' .\")  # Use grep tool instead"
_EXECUTE_TOOL_DESCRIPTION_TEMPLATE = """Executes a shell command in an isolated sandbox and returns combined stdout/stderr with the exit code (truncated if very large).

Usage:
- Quote paths containing spaces (e.g. cd "/path/with spaces").
- Chain commands with ';' or '&&' (use '&&' when a command depends on the previous); do not use newlines except inside quoted strings.
- Use absolute paths and avoid `cd` so the working directory stays stable; use the optional timeout to override the default.
- {search_guidance}Use read_file rather than cat/head/tail.{glob_bad_example}{grep_bad_example}

Only available on backends implementing SandboxBackendProtocol; otherwise it returns an error."""
def _execute_blurb(search_guidance: str, glob_bad_example: str, grep_bad_example: str) -> str:
    return _EXECUTE_TOOL_DESCRIPTION_TEMPLATE.format(search_guidance=search_guidance, glob_bad_example=glob_bad_example, grep_bad_example=grep_bad_example)


EXECUTE_TOOL_DESCRIPTION = _execute_blurb(_EXECUTE_SEARCH_GUIDANCE, _EXECUTE_GLOB_BAD_EXAMPLE, _EXECUTE_GREP_BAD_EXAMPLE)
_EXECUTE_TOOL_DESCRIPTION_WITH_GREP_ONLY = _execute_blurb(_EXECUTE_GREP_SEARCH_GUIDANCE, "", _EXECUTE_GREP_BAD_EXAMPLE)
_EXECUTE_TOOL_DESCRIPTION_WITH_GLOB_ONLY = _execute_blurb(_EXECUTE_GLOB_SEARCH_GUIDANCE, _EXECUTE_GLOB_BAD_EXAMPLE, "")
_EXECUTE_TOOL_DESCRIPTION_WITHOUT_SEARCH = _execute_blurb("", "", "")
FsToolName = Literal["ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"]
_FS_TOOL_ORDER: tuple[str, ...] = ("ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep")
_ALL_FS_TOOL_NAMES: frozenset[str] = frozenset(_FS_TOOL_ORDER) | {"execute"}


def _route_host_path_prompt(backend: contracts.BackendProtocol) -> str:
    """execute 跑在 default 的 shell 上。只有本机 shell 才把 FilesystemBackend 路由翻成宿主路径。"""
    if not isinstance(backend, composite_api.CompositeBackend):
        return ""
    default_uses_local_shell = isinstance(backend.default, LocalShellBackend)
    host_mappings: list[tuple[str, str]] = []
    no_host_routes: list[str] = []
    for route_prefix, route_backend in backend.sorted_routes:
        usable = default_uses_local_shell and isinstance(route_backend, host_files.FilesystemBackend)
        if not usable:
            no_host_routes.append(route_prefix)
            continue
        if route_backend.virtual_mode:
            host_mappings.append((route_prefix, str(route_backend.cwd)))
        else:
            host_mappings.append((route_prefix, "/"))
    if not host_mappings and not no_host_routes:
        return ""

    def slash(prefix: str) -> str:
        return prefix if prefix.endswith("/") else f"{prefix}/"

    def mapping_line(virtual_prefix: str, host_prefix: str) -> str:
        mounted = slash(virtual_prefix)
        host = slash(host_prefix)
        sample = f"`{mounted}dir/x.py` -> `{host}dir/x.py`"
        return f"- `{mounted}` -> `{host}` (e.g. {sample})"

    title = "## Shell paths vs. virtual paths"
    shell = "The `execute` tool runs commands in the host shell and can only access files that exist on the host filesystem."
    mounts = "Some paths returned by the file tools are virtual mounts:"
    mapped = "- If a virtual mount has a host path mapping, replace its virtual prefix with the host prefix when running shell commands."
    unmapped = (
        "- If a virtual mount does not have a host path mapping, it is not accessible "
        "from the shell. Use the file tools listed above to interact with those files."
    )
    caution = "Do not assume that a path returned by a file tool can be used directly in a shell command."
    lines = [title, "", shell, "", mounts, "", mapped, unmapped, "", caution]
    if host_mappings:
        mapped_rows = [mapping_line(virtual_prefix, host_prefix) for virtual_prefix, host_prefix in host_mappings]
        lines.extend(["", "Host path mappings:", *mapped_rows])
    if no_host_routes:
        hidden = "Virtual mounts without a host path mapping (not accessible from the shell):"
        bullet_rows = [f"- `{prefix}`" for prefix in no_host_routes]
        lines.extend(["", hidden, *bullet_rows])
    return "\n".join(lines)


def supports_execution(backend: contracts.BackendProtocol) -> bool:
    """composite 只看 default 会不会执行。"""
    if isinstance(backend, composite_api.CompositeBackend):
        return isinstance(backend.default, contracts.SandboxBackendProtocol)
    return isinstance(backend, contracts.SandboxBackendProtocol)


TOOLS_EXCLUDED_FROM_EVICTION = ("ls", "glob", "grep", "read_file", "edit_file", "write_file", "delete")
TOO_LARGE_HUMAN_MSG = """Message content too large and was saved to the filesystem at: {file_path}

You can read the full content using the read_file tool with pagination (offset and limit parameters).

Here is a preview showing the head and tail of the content:

{content_sample}
"""


def _build_evicted_human_content(message: HumanMessage, replacement_text: str) -> str | list[Any]:
    if isinstance(message.content, str):
        return replacement_text
    media_blocks = [block for block in message.content_blocks if block["type"] != "text"]
    if not media_blocks:
        return replacement_text
    return [{"type": "text", "text": replacement_text}, *media_blocks]


def _build_truncated_human_message(message: HumanMessage, file_path: str) -> HumanMessage:
    content_str = _extract_text_from_message(message)
    content_sample = _create_content_preview(content_str)
    replacement_text = TOO_LARGE_HUMAN_MSG.format(file_path=file_path, content_sample=content_sample)
    evicted = _build_evicted_human_content(message, replacement_text)
    return message.model_copy(update={"content": evicted})


class FilesystemMiddleware(agent_types.AgentMiddleware):
    """八个文件工具，加上按后端能力改提示、把过大的消息赶到文件系统。"""

    trace_policy = agent_types.TracePolicy(process_inputs=agent_types.omit_payload)
    state_schema: type[FilesystemState]

    def __init__(self, *, backend: contracts.BackendProtocol | None = None, system_prompt: str | None = None, custom_tool_descriptions: Mapping[str, str] | None = None, tool_token_limit_before_evict: int | None = 20000, human_message_token_limit_before_evict: int | None = 50000, max_execute_timeout: int = 3600, grep_max_count: int | None = 1000, tools: list[FsToolName] | Literal["all"] | None = None, _permissions: list[FilesystemPermission] | None = None) -> None:
        if isinstance(tools, list) and "read_file" not in tools:
            raise ValueError("read_file must be included in tools; it is required by FilesystemMiddleware")
        if max_execute_timeout <= 0:
            raise ValueError(f"max_execute_timeout must be positive, got {max_execute_timeout}")
        if grep_max_count is not None and grep_max_count <= 0:
            raise ValueError(f"grep_max_count must be positive or None, got {grep_max_count}")
        self.backend = backend if backend is not None else state_api.StateBackend()
        if callable(self.backend) and not isinstance(self.backend, contracts.BackendProtocol):
            raise TypeError(
                "backend must be an initialized backend instance. Backend factories "
                "were removed in deepagents 0.7; pass StateBackend(), "
                "CompositeBackend(...), or another BackendProtocol instance instead."
            )
        self.state_schema = cast(
            "type[FilesystemState]",
            FilesystemState if _uses_state_backend(self.backend) else agent_types.AgentState,
        )
        if _permissions and supports_execution(self.backend) and not _all_paths_scoped_to_routes(_permissions, self.backend):
            raise NotImplementedError(
                "FilesystemMiddleware does not yet support permissions with backends that "
                "provide command execution (SandboxBackendProtocol). Tool-level permissions "
                "for the execute tool are not implemented. Either remove permissions or use "
                "a backend without execution support."
            )
        artifacts_root = self.backend.artifacts_root if isinstance(self.backend, composite_api.CompositeBackend) else "/"
        root = artifacts_root.rstrip("/")
        self._large_tool_results_prefix = f"{root}/large_tool_results"
        self._conversation_history_prefix = f"{root}/conversation_history"
        self._custom_system_prompt = system_prompt
        self._custom_tool_descriptions = (custom_tool_descriptions or {})
        self._tool_token_limit_before_evict = tool_token_limit_before_evict
        self._human_message_token_limit_before_evict = human_message_token_limit_before_evict
        self._max_execute_timeout = max_execute_timeout
        self._grep_max_count = grep_max_count
        enabled: frozenset[str] | None
        if isinstance(tools, list):
            enabled = frozenset(tools)
        elif tools == "all":
            enabled = frozenset(_ALL_FS_TOOL_NAMES)
        else:
            enabled = None
        self._enabled_tools = enabled
        self._permissions = list(_permissions or [])
        self._glob_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_SYNC_GLOB_WORKERS, thread_name_prefix="deepagents-glob")
        self._glob_slots = threading.BoundedSemaphore(_SYNC_GLOB_WORKERS)
        builders = {
            "ls": self._create_ls_tool,
            "read_file": self._create_read_file_tool,
            "write_file": self._create_write_file_tool,
            "edit_file": self._create_edit_file_tool,
            "delete": self._create_delete_tool,
            "glob": self._create_glob_tool,
            "grep": self._create_grep_tool,
            "execute": self._create_execute_tool,
        }
        chosen = [name for name in (*_FS_TOOL_ORDER, "execute") if enabled is None or name in enabled]
        self.tools = [builders[name]() for name in chosen]

    def _path_or_error(self, raw: str, tool_name: str, tool_call_id: str | None) -> tuple[str | None, ToolMessage | None]:
        try:
            cleaned = validate_path(raw)
        except ValueError as exc:
            return None, _tool_error(tool_name, tool_call_id, f"Error: {exc}")
        return cleaned, None

    def _denied(self, tool_name: str, tool_call_id: str | None, operation: str, path: str, suffix: str = "") -> ToolMessage:
        if operation == "read":
            text = f"Error: permission denied for read on {path}{suffix}"
        else:
            text = f"Error: permission denied for write on {path}{suffix}"
        return _tool_error(tool_name, tool_call_id, text)

    def _create_ls_tool(self) -> BaseTool:
        description = self._custom_tool_descriptions.get("ls") or LIST_FILES_TOOL_DESCRIPTION

        def finish(path: str, tool_call_id: str | None, listing: contracts.LsResult) -> ToolMessage:
            if listing.error:
                return _tool_error("ls", tool_call_id, f"Error: {listing.error}")
            paths = _apply_permissions_to_ls_results(self._permissions, listing.entries or [])
            return ToolMessage(content=_format_file_paths(paths), tool_call_id=tool_call_id, name="ls", status="success")

        def sync_ls(runtime: ToolRuntime[None, FilesystemState], path: str) -> ToolMessage:
            """Synchronous wrapper for ls tool."""
            cleaned, failed = self._path_or_error(path, "ls", runtime.tool_call_id)
            if failed is not None or cleaned is None:
                return cast(ToolMessage, failed)
            if _check_fs_permission(self._permissions, "read", cleaned) == "deny":
                return self._denied("ls", runtime.tool_call_id, "read", cleaned)
            return finish(cleaned, runtime.tool_call_id, self.backend.ls(cleaned))

        async def async_ls(runtime: ToolRuntime[None, FilesystemState], path: str) -> ToolMessage:
            """Asynchronous wrapper for ls tool."""
            cleaned, failed = self._path_or_error(path, "ls", runtime.tool_call_id)
            if failed is not None or cleaned is None:
                return cast(ToolMessage, failed)
            if _check_fs_permission(self._permissions, "read", cleaned) == "deny":
                return self._denied("ls", runtime.tool_call_id, "read", cleaned)
            return finish(cleaned, runtime.tool_call_id, await self.backend.als(cleaned))

        return StructuredTool.from_function(name="ls", description=description, func=sync_ls, coroutine=async_ls, infer_schema=False, args_schema=LsSchema)

    def _create_read_file_tool(self) -> BaseTool:
        video_enabled = video_dependencies_available()
        default_description = READ_FILE_VIDEO_TOOL_DESCRIPTION if video_enabled else READ_FILE_TOOL_DESCRIPTION
        description = self._custom_tool_descriptions.get("read_file") or default_description
        args_schema = ReadVideoFileSchema if video_enabled else ReadFileSchema
        token_limit = self._tool_token_limit_before_evict

        def present(read_result: contracts.ReadResult, validated_path: str, tool_call_id: str | None, offset: int, limit: int) -> ToolMessage | Command:
            if read_result.error:
                return _tool_error("read_file", tool_call_id, f"Error: {read_result.error}")
            if read_result.file_data is None:
                return _tool_error("read_file", tool_call_id, f"Error: no data returned for '{validated_path}'")
            kind = _read_kind(validated_path, video_enabled=video_enabled)
            encoding = read_result.file_data.get("encoding", "utf-8")
            content = read_result.file_data["content"]
            empty_msg = check_empty_content(content)
            whole = read_result.start_line is None or (read_result.start_line == 1 and read_result.total_lines == read_result.end_line)
            if empty_msg and whole:
                if not content and read_result.no_lines_requested:
                    empty_msg = NO_LINES_REQUESTED_WARNING.format(limit=limit)
                return ToolMessage(content=empty_msg, name="read_file", tool_call_id=tool_call_id, status="success")
            if video_enabled and kind == "video":
                return _handle_video_read(content, validated_path, tool_call_id, offset, limit)
            if encoding == "base64" or kind != "text":
                block_type = kind if kind != "text" else "file"
                mime_type = mimetypes.guess_type("file" + HostPath(validated_path).suffix)[0] or "application/octet-stream"
                return ToolMessage(
                    content_blocks=cast("list", [{"type": block_type, "base64": content, "mime_type": mime_type}]),
                    name="read_file",
                    tool_call_id=tool_call_id,
                    additional_kwargs={"read_file_path": validated_path, "read_file_media_type": mime_type},
                    status="success",
                )
            prepared, body = _prepare_read_window(read_result, content, offset)
            clamp_notice = _clamped_offset_notice(offset).strip()
            notices = [clamp_notice] if clamp_notice else []
            rendered = _truncate_paginated_read(body, validated_path, prepared, token_limit, notices=notices)
            return ToolMessage(content=rendered, name="read_file", tool_call_id=tool_call_id, status="success")

        def sync_read_file(file_path: str, runtime: ToolRuntime[None, FilesystemState], offset: int = DEFAULT_READ_OFFSET, limit: int = DEFAULT_READ_LIMIT) -> ToolMessage | Command:
            """Synchronous wrapper for read_file tool."""
            cleaned, failed = self._path_or_error(file_path, "read_file", runtime.tool_call_id)
            if failed is not None or cleaned is None:
                return cast(ToolMessage, failed)
            if _check_fs_permission(self._permissions, "read", cleaned) == "deny":
                return self._denied("read_file", runtime.tool_call_id, "read", cleaned)
            found = self.backend.read(cleaned, offset=offset, limit=limit)
            return present(found, cleaned, runtime.tool_call_id, offset, limit)

        async def async_read_file(file_path: str, runtime: ToolRuntime[None, FilesystemState], offset: int = DEFAULT_READ_OFFSET, limit: int = DEFAULT_READ_LIMIT) -> ToolMessage | Command:
            """Asynchronous wrapper for read_file tool."""
            cleaned, failed = self._path_or_error(file_path, "read_file", runtime.tool_call_id)
            if failed is not None or cleaned is None:
                return cast(ToolMessage, failed)
            if _check_fs_permission(self._permissions, "read", cleaned) == "deny":
                return self._denied("read_file", runtime.tool_call_id, "read", cleaned)
            found = await self.backend.aread(cleaned, offset=offset, limit=limit)
            return present(found, cleaned, runtime.tool_call_id, offset, limit)

        return StructuredTool.from_function(name="read_file", description=description, func=sync_read_file, coroutine=async_read_file, infer_schema=False, args_schema=args_schema)

    def _create_write_file_tool(self) -> BaseTool:
        description = self._custom_tool_descriptions.get("write_file") or WRITE_FILE_TOOL_DESCRIPTION

        def finish(result: contracts.WriteResult, tool_call_id: str | None) -> ToolMessage:
            if result.error:
                return ToolMessage(content=result.error, name="write_file", tool_call_id=tool_call_id, status="error")
            return ToolMessage(content=f"Updated file {result.path}", name="write_file", tool_call_id=tool_call_id, status="success")

        def sync_write_file(file_path: str, content: str, runtime: ToolRuntime[None, FilesystemState]) -> ToolMessage:
            """Synchronous wrapper for write_file tool."""
            cleaned, failed = self._path_or_error(file_path, "write_file", runtime.tool_call_id)
            if failed is not None or cleaned is None:
                return cast(ToolMessage, failed)
            if _check_fs_permission(self._permissions, "write", cleaned) == "deny":
                return self._denied("write_file", runtime.tool_call_id, "write", cleaned)
            return finish(self.backend.write(cleaned, content), runtime.tool_call_id)

        async def async_write_file(file_path: str, content: str, runtime: ToolRuntime[None, FilesystemState]) -> ToolMessage:
            """Asynchronous wrapper for write_file tool."""
            cleaned, failed = self._path_or_error(file_path, "write_file", runtime.tool_call_id)
            if failed is not None or cleaned is None:
                return cast(ToolMessage, failed)
            if _check_fs_permission(self._permissions, "write", cleaned) == "deny":
                return self._denied("write_file", runtime.tool_call_id, "write", cleaned)
            return finish(await self.backend.awrite(cleaned, content), runtime.tool_call_id)

        return StructuredTool.from_function(name="write_file", description=description, func=sync_write_file, coroutine=async_write_file, infer_schema=False, args_schema=WriteFileSchema)

    def _create_edit_file_tool(self) -> BaseTool:
        description = self._custom_tool_descriptions.get("edit_file") or EDIT_FILE_TOOL_DESCRIPTION

        def finish(result: contracts.EditResult, tool_call_id: str | None) -> ToolMessage:
            if result.error:
                return ToolMessage(content=result.error, name="edit_file", tool_call_id=tool_call_id, status="error")
            text = f"Successfully replaced {result.occurrences} instance(s) of the string in '{result.path}'"
            return ToolMessage(content=text, name="edit_file", tool_call_id=tool_call_id, status="success")

        def sync_edit_file(file_path: str, old_string: str, new_string: str, runtime: ToolRuntime[None, FilesystemState], *, replace_all: bool = False) -> ToolMessage:
            """Synchronous wrapper for edit_file tool."""
            cleaned, failed = self._path_or_error(file_path, "edit_file", runtime.tool_call_id)
            if failed is not None or cleaned is None:
                return cast(ToolMessage, failed)
            if _check_fs_permission(self._permissions, "write", cleaned) == "deny":
                return self._denied("edit_file", runtime.tool_call_id, "write", cleaned)
            return finish(self.backend.edit(cleaned, old_string, new_string, replace_all=replace_all), runtime.tool_call_id)

        async def async_edit_file(file_path: str, old_string: str, new_string: str, runtime: ToolRuntime[None, FilesystemState], *, replace_all: bool = False) -> ToolMessage:
            """Asynchronous wrapper for edit_file tool."""
            cleaned, failed = self._path_or_error(file_path, "edit_file", runtime.tool_call_id)
            if failed is not None or cleaned is None:
                return cast(ToolMessage, failed)
            if _check_fs_permission(self._permissions, "write", cleaned) == "deny":
                return self._denied("edit_file", runtime.tool_call_id, "write", cleaned)
            edited = await self.backend.aedit(cleaned, old_string, new_string, replace_all=replace_all)
            return finish(edited, runtime.tool_call_id)

        return StructuredTool.from_function(name="edit_file", description=description, func=sync_edit_file, coroutine=async_edit_file, infer_schema=False, args_schema=EditFileSchema)

    def _create_delete_tool(self) -> BaseTool:
        description = self._custom_tool_descriptions.get("delete") or DELETE_TOOL_DESCRIPTION

        def blocked(path: str, patterns: list[str], tool_call_id: str | None) -> ToolMessage | None:
            if not patterns:
                return None
            joined = ", ".join(patterns)
            text = f"Error: permission denied for write on {path} (matches deny rule(s): {joined})"
            return _tool_error("delete", tool_call_id, text)

        def finish(result: contracts.DeleteResult, tool_call_id: str | None) -> ToolMessage:
            if result.error:
                return ToolMessage(content=result.error, name="delete", tool_call_id=tool_call_id, status="error")
            return ToolMessage(content=f"Deleted {result.path}", name="delete", tool_call_id=tool_call_id, status="success")

        def sync_delete(file_path: str, runtime: ToolRuntime[None, FilesystemState]) -> ToolMessage:
            """Synchronous wrapper for delete tool."""
            cleaned, failed = self._path_or_error(file_path, "delete", runtime.tool_call_id)
            if failed is not None or cleaned is None:
                return cast(ToolMessage, failed)
            descendants = _delete_target_may_have_descendants(self.backend, cleaned, permissions_configured=bool(self._permissions))
            patterns = _find_delete_deny_patterns(self._permissions, cleaned, has_descendants=descendants)
            refusal = blocked(cleaned, patterns, runtime.tool_call_id)
            if refusal is not None:
                return refusal
            return finish(self.backend.delete(cleaned), runtime.tool_call_id)

        async def async_delete(file_path: str, runtime: ToolRuntime[None, FilesystemState]) -> ToolMessage:
            """Asynchronous wrapper for delete tool."""
            cleaned, failed = self._path_or_error(file_path, "delete", runtime.tool_call_id)
            if failed is not None or cleaned is None:
                return cast(ToolMessage, failed)
            descendants = await _adelete_target_may_have_descendants(self.backend, cleaned, permissions_configured=bool(self._permissions))
            patterns = _find_delete_deny_patterns(self._permissions, cleaned, has_descendants=descendants)
            refusal = blocked(cleaned, patterns, runtime.tool_call_id)
            if refusal is not None:
                return refusal
            return finish(await self.backend.adelete(cleaned), runtime.tool_call_id)

        return StructuredTool.from_function(name="delete", description=description, func=sync_delete, coroutine=async_delete, infer_schema=False, args_schema=DeleteSchema)

    def _glob_message(self, glob_result: contracts.GlobResult, tool_call_id: str | None) -> ToolMessage:
        if glob_result.error:
            return _tool_error("glob", tool_call_id, f"Error: {glob_result.error}")
        paths = _apply_permissions_to_glob_results(self._permissions, glob_result.matches or [])
        content = _format_glob_tool_result(paths, truncated=glob_result.truncated, truncation_reason=glob_result.truncation_reason)
        return ToolMessage(content=content, tool_call_id=tool_call_id, name="glob", status="success")

    def _create_glob_tool(self) -> BaseTool:
        description = self._custom_tool_descriptions.get("glob") or GLOB_TOOL_DESCRIPTION

        def sync_glob(pattern: str, runtime: ToolRuntime[None, FilesystemState], path: str | None = None) -> ToolMessage:
            """Synchronous wrapper for glob tool."""
            anchor = path if path is not None else "/"
            cleaned, failed = self._path_or_error(anchor, "glob", runtime.tool_call_id)
            if failed is not None or cleaned is None:
                return cast(ToolMessage, failed)
            if _check_fs_permission(self._permissions, "read", cleaned) == "deny":
                suffix = GLOB_PATHLESS_DENIED_HINT if path is None else ""
                return self._denied("glob", runtime.tool_call_id, "read", cleaned, suffix)
            backend_path = cleaned if path is not None else None
            outcome = self._submit_glob(pattern, backend_path)
            if outcome == "saturated":
                return _tool_error("glob", runtime.tool_call_id, "Error: too many glob calls are already running. Try again later with a more specific pattern or a narrower path.")
            if outcome == "timeout":
                return _tool_error("glob", runtime.tool_call_id, _glob_timeout_message())
            if isinstance(outcome, BaseException):
                return _tool_error("glob", runtime.tool_call_id, f"Error: glob failed: {outcome}")
            return self._glob_message(outcome, runtime.tool_call_id)

        async def async_glob(pattern: str, runtime: ToolRuntime[None, FilesystemState], path: str | None = None) -> ToolMessage:
            """Asynchronous wrapper for glob tool."""
            anchor = path if path is not None else "/"
            cleaned, failed = self._path_or_error(anchor, "glob", runtime.tool_call_id)
            if failed is not None or cleaned is None:
                return cast(ToolMessage, failed)
            if _check_fs_permission(self._permissions, "read", cleaned) == "deny":
                suffix = GLOB_PATHLESS_DENIED_HINT if path is None else ""
                return self._denied("glob", runtime.tool_call_id, "read", cleaned, suffix)
            backend_path = cleaned if path is not None else None
            task = asyncio.ensure_future(self.backend.aglob(pattern, path=backend_path))
            done, _pending = await asyncio.wait({task}, timeout=GLOB_TIMEOUT)
            if not done:
                task.add_done_callback(_discard_task_result)
                task.cancel()
                return _tool_error("glob", runtime.tool_call_id, _glob_timeout_message())
            try:
                glob_result = task.result()
            except Exception as exc:
                return _tool_error("glob", runtime.tool_call_id, f"Error: glob failed: {exc}")
            return self._glob_message(glob_result, runtime.tool_call_id)

        return StructuredTool.from_function(name="glob", description=description, func=sync_glob, coroutine=async_glob, infer_schema=False, args_schema=GlobSchema)

    def _submit_glob(self, pattern: str, backend_path: str | None) -> Any:
        if not self._glob_slots.acquire(blocking=False):
            return "saturated"
        copied = contextvars.copy_context()

        def run_glob() -> contracts.GlobResult:
            try:
                outcome = copied.run(self.backend.glob, pattern, path=backend_path)
            finally:
                self._glob_slots.release()
            return outcome

        try:
            future = self._glob_executor.submit(run_glob)
        except Exception as boom:
            self._glob_slots.release()
            raise boom
        done, _pending = concurrent.futures.wait([future], timeout=GLOB_TIMEOUT)
        if not done:
            if future.cancel():
                self._glob_slots.release()
            return "timeout"
        try:
            return future.result()
        except Exception as exc:
            return exc

    def _create_grep_tool(self) -> BaseTool:
        description = self._grep_tool_description(include_execution=True)

        def render(grep_result: contracts.GrepResult, pattern: str, output_mode: Literal["files_with_matches", "content", "count"], tool_call_id: str | None) -> ToolMessage:
            matches = grep_result.matches or []
            filtered = _filter_grep_matches_by_permission(self._permissions, matches, operation="read")
            narrowed = contracts.GrepResult(error=grep_result.error, matches=filtered, truncated=grep_result.truncated)
            formatted, status = _format_grep_tool_result(narrowed, output_mode, pattern, backend_had_matches=bool(matches))
            return ToolMessage(content=formatted, tool_call_id=tool_call_id, name="grep", status=status)

        def sync_grep(pattern: str, runtime: ToolRuntime[None, FilesystemState], path: str | None = None, glob: str | None = None, output_mode: Literal["files_with_matches", "content", "count"] = "files_with_matches", max_count: int | None = None) -> ToolMessage:
            """Synchronous wrapper for grep tool."""
            if path is not None:
                cleaned, failed = self._path_or_error(path, "grep", runtime.tool_call_id)
                if failed is not None or cleaned is None:
                    return cast(ToolMessage, failed)
                if _check_fs_permission(self._permissions, "read", cleaned) == "deny":
                    return self._denied("grep", runtime.tool_call_id, "read", cleaned)
                path = cleaned
            effective = max_count if max_count is not None else self._grep_max_count
            found = _grep_backend(self.backend, pattern, path, glob, effective)
            return render(found, pattern, output_mode, runtime.tool_call_id)

        async def async_grep(pattern: str, runtime: ToolRuntime[None, FilesystemState], path: str | None = None, glob: str | None = None, output_mode: Literal["files_with_matches", "content", "count"] = "files_with_matches", max_count: int | None = None) -> ToolMessage:
            """Asynchronous wrapper for grep tool."""
            if path is not None:
                cleaned, failed = self._path_or_error(path, "grep", runtime.tool_call_id)
                if failed is not None or cleaned is None:
                    return cast(ToolMessage, failed)
                if _check_fs_permission(self._permissions, "read", cleaned) == "deny":
                    return self._denied("grep", runtime.tool_call_id, "read", cleaned)
                path = cleaned
            effective = max_count if max_count is not None else self._grep_max_count
            found = await _agrep_backend(self.backend, pattern, path, glob, effective)
            return render(found, pattern, output_mode, runtime.tool_call_id)

        return StructuredTool.from_function(name="grep", description=description, func=sync_grep, coroutine=async_grep, infer_schema=False, args_schema=GrepSchema)

    def _grep_tool_description(self, *, include_execution: bool) -> str:
        custom = self._custom_tool_descriptions.get("grep")
        if custom:
            return custom
        if include_execution:
            return GREP_TOOL_DESCRIPTION
        return _GREP_TOOL_DESCRIPTION_WITHOUT_EXECUTE

    def _rewrite_description(self, tools: list[BaseTool | dict[str, Any]], *, tool_name: str, target: str, defaults: set[str]) -> list[BaseTool | dict[str, Any]]:
        rewritten: list[BaseTool | dict[str, Any]] = []
        changed = False
        for tool in tools:
            name = self._tool_name(tool)
            if name != tool_name:
                rewritten.append(tool)
                continue
            if isinstance(tool, BaseTool):
                stale = tool.description in defaults and tool.description != target
                rewritten.append(tool.model_copy(update={"description": target}) if stale else tool)
                changed = changed or stale
                continue
            if not isinstance(tool, dict):
                rewritten.append(cast("BaseTool | dict[str, Any]", tool))
                continue
            if tool.get("description") in defaults and tool.get("description") != target:
                copied = tool.copy()
                copied["description"] = target
                rewritten.append(copied)
                changed = True
            else:
                rewritten.append(tool)
        if changed:
            return rewritten
        return tools

    def _with_filtered_grep_description(self, tools: list[BaseTool | dict[str, Any]], *, include_execution: bool) -> list[BaseTool | dict[str, Any]]:
        if self._custom_tool_descriptions.get("grep"):
            return tools
        target = self._grep_tool_description(include_execution=include_execution)
        defaults = {GREP_TOOL_DESCRIPTION, _GREP_TOOL_DESCRIPTION_WITHOUT_EXECUTE}
        return self._rewrite_description(tools, tool_name="grep", target=target, defaults=defaults)

    def _execute_tool_description(self, *, visible_search_tools: set[str]) -> str:
        custom = self._custom_tool_descriptions.get("execute")
        if custom:
            return custom
        has_grep = "grep" in visible_search_tools
        has_glob = "glob" in visible_search_tools
        if has_grep and has_glob:
            return EXECUTE_TOOL_DESCRIPTION
        if has_grep:
            return _EXECUTE_TOOL_DESCRIPTION_WITH_GREP_ONLY
        if has_glob:
            return _EXECUTE_TOOL_DESCRIPTION_WITH_GLOB_ONLY
        return _EXECUTE_TOOL_DESCRIPTION_WITHOUT_SEARCH

    def _with_filtered_execute_description(self, tools: list[BaseTool | dict[str, Any]], *, visible_search_tools: set[str]) -> list[BaseTool | dict[str, Any]]:
        if self._custom_tool_descriptions.get("execute"):
            return tools
        target = self._execute_tool_description(visible_search_tools=visible_search_tools)
        defaults = {
            EXECUTE_TOOL_DESCRIPTION,
            _EXECUTE_TOOL_DESCRIPTION_WITH_GREP_ONLY,
            _EXECUTE_TOOL_DESCRIPTION_WITH_GLOB_ONLY,
            _EXECUTE_TOOL_DESCRIPTION_WITHOUT_SEARCH,
        }
        return self._rewrite_description(tools, tool_name="execute", target=target, defaults=defaults)

    @staticmethod
    def _tool_name(tool: object) -> str | None:
        if isinstance(tool, BaseTool):
            return tool.name
        if isinstance(tool, dict):
            return cast("str | None", tool.get("name"))
        if hasattr(tool, "name"):
            return cast("str | None", getattr(tool, "name"))
        getter = getattr(tool, "get", None)
        if callable(getter):
            return cast("str | None", getter("name"))
        return None

    def _unsupported_tools_and_execution_state(self, tool_names: set[str | None]) -> tuple[set[str | None], bool, contracts.BackendProtocol | None]:
        unsupported: set[str | None] = set()
        execution_active = False
        backend = None
        has_execute = "execute" in tool_names
        has_delete = "delete" in tool_names
        if not has_delete and not has_execute:
            return unsupported, execution_active, backend
        backend = self.backend
        if has_execute and "execute" not in unsupported:
            execution_active = supports_execution(backend)
            if not execution_active:
                unsupported.add("execute")
        if has_delete and "delete" not in unsupported and not contracts._supports_delete(backend):
            unsupported.add("delete")
        return unsupported, execution_active, backend

    def _resolve_capture(self, resolved_backend: contracts.BackendProtocol, tool_call_id: str | None) -> tuple[BaseSandbox, str] | None:
        if not self._tool_token_limit_before_evict or not tool_call_id:
            return None
        capture_path = f"{self._large_tool_results_prefix}/{sanitize_tool_call_id(tool_call_id)}"
        if isinstance(resolved_backend, composite_api.CompositeBackend):
            default = resolved_backend.default
            if not isinstance(default, BaseSandbox):
                return None
            routed, _child, route_prefix = composite_api._route_for_path(
                default=default,
                sorted_routes=resolved_backend.sorted_routes,
                path=capture_path,
            )
            same_default = route_prefix is None and routed is default
            if same_default:
                return (default, capture_path)
            return None
        if not isinstance(resolved_backend, BaseSandbox):
            return None
        return (resolved_backend, capture_path)

    @staticmethod
    def _format_execute_output(output: str, exit_code: int | None, *, truncated: bool) -> str:
        chunks = [output]
        if exit_code is not None:
            status = "succeeded" if exit_code == 0 else "failed"
            chunks.append(f"\n[Command {status} with exit code {exit_code}]")
        if truncated:
            chunks.append("\n[Output was truncated due to size limits]")
        return "".join(chunks)

    @staticmethod
    def _execute_artifact(response: contracts.ExecuteResponse) -> contracts.ExecuteArtifact:
        if response.exit_code is None:
            return {}
        return {"exit_code": response.exit_code}

    def _interpret_capture_output(self, offload: contracts.ExecuteOffloadResult, capture_path: str, tool_call_id: str) -> str:
        response = offload.response
        if not offload.offloaded:
            return self._format_execute_output(response.output, response.exit_code, truncated=response.truncated)
        status = "succeeded" if response.exit_code == 0 else "failed"
        status_line = f"[Command {status} with exit code {response.exit_code}]"
        if response.truncated:
            status_line += "\n[Output exceeded the capture size limit and was truncated; the saved file is incomplete]"
        content_sample = f"{status_line}\n{response.output}"
        return TOO_LARGE_TOOL_MSG.format(tool_call_id=tool_call_id, file_path=capture_path, content_sample=content_sample)

    def _reject_timeout(self, timeout: int | None, tool_call_id: str | None) -> ToolMessage | None:
        if timeout is None:
            return None
        if timeout < 0:
            return _tool_error("execute", tool_call_id, f"Error: timeout must be non-negative, got {timeout}.")
        if timeout > self._max_execute_timeout:
            return _tool_error("execute", tool_call_id, f"Error: timeout {timeout}s exceeds maximum allowed ({self._max_execute_timeout}s).")
        return None

    def _create_execute_tool(self) -> BaseTool:
        visible = {"grep", "glob"}
        if self._enabled_tools is not None:
            visible.intersection_update(self._enabled_tools)
        description = self._execute_tool_description(visible_search_tools=visible)
        unavailable = (
            "Error: Execution not available. This agent's backend "
            "does not support command execution (SandboxBackendProtocol). "
            "To use the execute tool, provide a backend that implements SandboxBackendProtocol."
        )
        no_timeout = (
            "Error: This sandbox backend does not support per-command "
            "timeout overrides. Update your sandbox package to the "
            "latest version, or omit the timeout parameter."
        )

        def sync_execute(command: str, runtime: ToolRuntime[None, FilesystemState], timeout: int | None = None) -> ToolMessage:
            """Synchronous wrapper for execute tool."""
            rejected = self._reject_timeout(timeout, runtime.tool_call_id)
            if rejected is not None:
                return rejected
            resolved = self.backend
            if not supports_execution(resolved):
                return _tool_error("execute", runtime.tool_call_id, unavailable)
            executable = cast("contracts.SandboxBackendProtocol", resolved)
            if timeout is not None and not contracts.execute_accepts_timeout(type(executable)):
                return _tool_error("execute", runtime.tool_call_id, no_timeout)
            capture = self._resolve_capture(resolved, runtime.tool_call_id)
            try:
                if capture is not None:
                    executor, capture_path = capture
                    budget = NUM_CHARS_PER_TOKEN * cast("int", self._tool_token_limit_before_evict)
                    offload = executor.execute_with_offload(command, capture_path, max_inline_bytes=budget, timeout=timeout)
                    response = offload.response
                    content = self._interpret_capture_output(offload, capture_path, cast("str", runtime.tool_call_id))
                elif timeout is not None:
                    response = executable.execute(command, timeout=timeout)
                    content = self._format_execute_output(response.output, response.exit_code, truncated=response.truncated)
                else:
                    response = executable.execute(command)
                    content = self._format_execute_output(response.output, response.exit_code, truncated=response.truncated)
            except NotImplementedError as exc:
                return _tool_error("execute", runtime.tool_call_id, f"Error: Execution not available. {exc}")
            except ValueError as exc:
                return _tool_error("execute", runtime.tool_call_id, f"Error: Invalid parameter. {exc}")
            return ToolMessage(content=content, name="execute", tool_call_id=runtime.tool_call_id, artifact=self._execute_artifact(response), status="success")

        async def async_execute(command: str, runtime: ToolRuntime[None, FilesystemState], timeout: int | None = None) -> ToolMessage:
            """Asynchronous wrapper for execute tool."""
            rejected = self._reject_timeout(timeout, runtime.tool_call_id)
            if rejected is not None:
                return rejected
            resolved = self.backend
            if not supports_execution(resolved):
                return _tool_error("execute", runtime.tool_call_id, unavailable)
            executable = cast("contracts.SandboxBackendProtocol", resolved)
            if timeout is not None and not contracts.execute_accepts_timeout(type(executable)):
                return _tool_error("execute", runtime.tool_call_id, no_timeout)
            capture = self._resolve_capture(resolved, runtime.tool_call_id)
            try:
                if capture is not None:
                    executor, capture_path = capture
                    budget = NUM_CHARS_PER_TOKEN * cast("int", self._tool_token_limit_before_evict)
                    offload = await executor.aexecute_with_offload(command, capture_path, max_inline_bytes=budget, timeout=timeout)
                    response = offload.response
                    content = self._interpret_capture_output(offload, capture_path, cast("str", runtime.tool_call_id))
                elif timeout is not None:
                    response = await executable.aexecute(command, timeout=timeout)
                    content = self._format_execute_output(response.output, response.exit_code, truncated=response.truncated)
                else:
                    response = await executable.aexecute(command)
                    content = self._format_execute_output(response.output, response.exit_code, truncated=response.truncated)
            except NotImplementedError as exc:
                return _tool_error("execute", runtime.tool_call_id, f"Error: Execution not available. {exc}")
            except ValueError as exc:
                return _tool_error("execute", runtime.tool_call_id, f"Error: Invalid parameter. {exc}")
            return ToolMessage(content=content, name="execute", tool_call_id=runtime.tool_call_id, artifact=self._execute_artifact(response), status="success")

        return StructuredTool.from_function(name="execute", description=description, func=sync_execute, coroutine=async_execute, infer_schema=False, args_schema=ExecuteSchema)

    def _filter_unsupported_tools_and_apply_prompt(self, request: agent_types.ModelRequest) -> agent_types.ModelRequest:
        names = {self._tool_name(tool) for tool in request.tools}
        unsupported, execution_active, backend = self._unsupported_tools_and_execution_state(names)
        visible = [tool for tool in request.tools if self._tool_name(tool) not in unsupported]
        visible_fs = {name for name in (names - unsupported) if name is not None}
        if unsupported:
            request = request.override(tools=visible)
        described = self._with_filtered_grep_description(visible, include_execution=execution_active)
        described = self._with_filtered_execute_description(described, visible_search_tools=visible_fs)
        if described is not visible:
            request = request.override(tools=described)
        parts = [self._custom_system_prompt] if self._custom_system_prompt else []
        if execution_active:
            route_prompt = _route_host_path_prompt(cast("contracts.BackendProtocol", backend))
            if route_prompt:
                parts.append(route_prompt)
        system_prompt = "\n\n".join(parts).strip()
        if system_prompt:
            request = request.override(system_message=append_to_system_message(request.system_message, system_prompt))
        return request

    def wrap_model_call(self, request: agent_types.ModelRequest, handler: Callable[[agent_types.ModelRequest], agent_types.ModelResponse]) -> agent_types.ModelResponse | agent_types.ExtendedModelResponse:
        request = self._filter_unsupported_tools_and_apply_prompt(request)
        prepared = _move_media_results_after_tool_results(list(request.messages))
        prepared = _scrub_unsupported_multimodal_content(prepared, request.model)
        if prepared != list(request.messages):
            request = request.override(messages=prepared)
        eviction = self._evict_and_truncate_messages(request)
        if eviction is None:
            return handler(request)
        messages, state_command = eviction
        request = request.override(messages=messages)
        response = handler(request)
        if state_command is not None:
            return agent_types.ExtendedModelResponse(model_response=response, command=state_command)
        return response

    async def awrap_model_call(self, request: agent_types.ModelRequest, handler: Callable[[agent_types.ModelRequest], Awaitable[agent_types.ModelResponse]]) -> agent_types.ModelResponse | agent_types.ExtendedModelResponse:
        request = self._filter_unsupported_tools_and_apply_prompt(request)
        prepared = _move_media_results_after_tool_results(list(request.messages))
        prepared = _scrub_unsupported_multimodal_content(prepared, request.model)
        if prepared != list(request.messages):
            request = request.override(messages=prepared)
        eviction = await self._aevict_and_truncate_messages(request)
        if eviction is None:
            return await handler(request)
        messages, state_command = eviction
        request = request.override(messages=messages)
        response = await handler(request)
        if state_command is not None:
            return agent_types.ExtendedModelResponse(model_response=response, command=state_command)
        return response

    def _process_large_message(self, message: ToolMessage, resolved_backend: contracts.BackendProtocol) -> tuple[ToolMessage, bool]:
        if not self._tool_token_limit_before_evict:
            return message, False
        content_str = _extract_text_from_message(message)
        if len(content_str) <= NUM_CHARS_PER_TOKEN * self._tool_token_limit_before_evict:
            return message, False
        processed = _offload_tool_message_content(message, content_str, resolved_backend, self._large_tool_results_prefix)
        if processed is None:
            return message, False
        return processed, True

    async def _aprocess_large_message(self, message: ToolMessage, resolved_backend: contracts.BackendProtocol) -> tuple[ToolMessage, bool]:
        if not self._tool_token_limit_before_evict:
            return message, False
        content_str = _extract_text_from_message(message)
        if len(content_str) <= NUM_CHARS_PER_TOKEN * self._tool_token_limit_before_evict:
            return message, False
        processed = await _aoffload_tool_message_content(message, content_str, resolved_backend, self._large_tool_results_prefix)
        if processed is None:
            return message, False
        return processed, True

    def _check_eviction_needed(self, messages: list[AnyMessage]) -> tuple[bool, bool]:
        limit = self._human_message_token_limit_before_evict
        if not limit:
            return False, False
        threshold = NUM_CHARS_PER_TOKEN * limit
        tagged = [msg for msg in messages if isinstance(msg, HumanMessage) and msg.additional_kwargs.get("lc_evicted_to")]
        fresh = False
        if messages and isinstance(messages[-1], HumanMessage):
            last = messages[-1]
            already = bool(last.additional_kwargs.get("lc_evicted_to"))
            fresh = (not already) and len(_extract_text_from_message(last)) > threshold
        return bool(tagged), fresh

    @staticmethod
    def _apply_eviction_and_truncate(messages: list[AnyMessage], write_result: contracts.WriteResult | None, file_path: str | None) -> tuple[list[AnyMessage], Command | None]:
        state_command: Command | None = None
        if write_result is not None and file_path is not None and not write_result.error:
            last = messages[-1]
            kept_id = last.id if last.id is not None else str(uuid.uuid4())
            kwargs = {**last.additional_kwargs, "lc_evicted_to": file_path}
            tagged = last.model_copy(update={"id": kept_id, "additional_kwargs": kwargs})
            state_command = Command(update={"messages": [tagged]})
            messages = [*messages[:-1], tagged]
        processed: list[AnyMessage] = []
        for msg in messages:
            saved = msg.additional_kwargs.get("lc_evicted_to") if isinstance(msg, HumanMessage) else None
            if isinstance(msg, HumanMessage) and saved:
                processed.append(_build_truncated_human_message(msg, saved))
            else:
                processed.append(msg)
        return processed, state_command

    def _evict_and_truncate_messages(self, request: agent_types.ModelRequest) -> tuple[list[AnyMessage], Command | None] | None:
        messages = list(request.messages)
        has_tagged, new_eviction_needed = self._check_eviction_needed(messages)
        if not has_tagged and not new_eviction_needed:
            return None
        write_result = None
        file_path = None
        if new_eviction_needed:
            file_path = f"{self._conversation_history_prefix}/{uuid.uuid4()}.md"
            write_result = self.backend.write(file_path, _extract_text_from_message(messages[-1]))
        return self._apply_eviction_and_truncate(messages, write_result, file_path)

    async def _aevict_and_truncate_messages(self, request: agent_types.ModelRequest) -> tuple[list[AnyMessage], Command | None] | None:
        messages = list(request.messages)
        has_tagged, new_eviction_needed = self._check_eviction_needed(messages)
        if not has_tagged and not new_eviction_needed:
            return None
        write_result = None
        file_path = None
        if new_eviction_needed:
            file_path = f"{self._conversation_history_prefix}/{uuid.uuid4()}.md"
            write_result = await self.backend.awrite(file_path, _extract_text_from_message(messages[-1]))
        return self._apply_eviction_and_truncate(messages, write_result, file_path)

    @staticmethod
    def _unwrap_command_messages(update: Mapping[str, Any]) -> tuple[Any, bool]:
        command_messages = update.get("messages", [])
        sentinel = (
            isinstance(command_messages, list)
            and bool(command_messages)
            and isinstance(command_messages[0], RemoveMessage)
            and command_messages[0].id == REMOVE_ALL_MESSAGES
        )
        if sentinel:
            return command_messages[1:], True
        return command_messages, False

    @staticmethod
    def _rewrap_command_messages(messages: list[AnyMessage], *, wrapped: bool) -> list[Any]:
        if wrapped:
            return [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]
        return list(messages)

    def _walk_command(self, tool_result: Command, processor) -> Command:
        update = tool_result.update
        if update is None:
            return tool_result
        command_messages, wrapped = self._unwrap_command_messages(update)
        processed = []
        for message in command_messages:
            if isinstance(message, ToolMessage):
                processed.append(processor(message))
            else:
                processed.append(message)
        new_messages = self._rewrap_command_messages(processed, wrapped=wrapped)
        return Command(goto=tool_result.goto, graph=tool_result.graph, update={**update, "messages": new_messages})

    def _intercept_large_tool_result(self, tool_result: ToolMessage | Command) -> ToolMessage | Command:
        if isinstance(tool_result, ToolMessage):
            processed, _evicted = self._process_large_message(tool_result, self.backend)
            return processed
        if isinstance(tool_result, Command):
            def processor(message: ToolMessage) -> ToolMessage:
                processed, _evicted = self._process_large_message(message, self.backend)
                return processed
            return self._walk_command(tool_result, processor)
        raise AssertionError(f"Unreachable code reached in _intercept_large_tool_result: for tool_result of type {type(tool_result)}")

    async def _aintercept_large_tool_result(self, tool_result: ToolMessage | Command) -> ToolMessage | Command:
        if isinstance(tool_result, ToolMessage):
            processed, _evicted = await self._aprocess_large_message(tool_result, self.backend)
            return processed
        if isinstance(tool_result, Command):
            processed_messages = []
            update = tool_result.update
            if update is None:
                return tool_result
            command_messages, wrapped = self._unwrap_command_messages(update)
            for message in command_messages:
                if not isinstance(message, ToolMessage):
                    processed_messages.append(message)
                    continue
                processed, _evicted = await self._aprocess_large_message(message, self.backend)
                processed_messages.append(processed)
            new_messages = self._rewrap_command_messages(processed_messages, wrapped=wrapped)
            return Command(goto=tool_result.goto, graph=tool_result.graph, update={**update, "messages": new_messages})
        raise AssertionError(f"Unreachable code reached in _aintercept_large_tool_result: for tool_result of type {type(tool_result)}")

    def _skip_tool_eviction(self, tool_name: str) -> bool:
        if self._tool_token_limit_before_evict is None:
            return True
        return tool_name in TOOLS_EXCLUDED_FROM_EVICTION

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command:
        produced = handler(request)
        if self._skip_tool_eviction(request.tool_call["name"]):
            return produced
        return self._intercept_large_tool_result(produced)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]) -> ToolMessage | Command:
        produced = await handler(request)
        if self._skip_tool_eviction(request.tool_call["name"]):
            return produced
        return await self._aintercept_large_tool_result(produced)
