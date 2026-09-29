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
"""包一层已经存在的 LangSmith Sandbox。

同步 ``read`` / ``write`` 走 SDK，免得把正文塞进命令行。``ls``、``edit``、
异步 ``aread`` 仍走 ``execute`` / ``aexecute``。捕获脚本由基类包，本类的
``execute`` 只把命令交出去。

本文件不读 API key。没有 dataplane 时 SDK 抛 ``DataplaneNotConfiguredError``，
各方法按自己的 ``except`` 决定是抛出还是收成结果。
"""

from __future__ import annotations

import base64
import logging

from . import protocol as contracts
from . import sandbox as remote_shell
from . import utils as text_tools

BaseSandbox = remote_shell.BaseSandbox
MAX_BINARY_BYTES = remote_shell.MAX_BINARY_BYTES
MAX_OUTPUT_BYTES = remote_shell.MAX_OUTPUT_BYTES
TRUNCATION_MSG = remote_shell.TRUNCATION_MSG

ExecuteResponse = contracts.ExecuteResponse
FileData = contracts.FileData
FileDownloadResponse = contracts.FileDownloadResponse
FileUploadResponse = contracts.FileUploadResponse
ReadResult = contracts.ReadResult
WriteResult = contracts.WriteResult

normalize_read_bounds = text_tools.normalize_read_bounds
_file_kind = text_tools._get_backend_read_file_type

logger = logging.getLogger(__name__)

_EMPTY_FILE_NOTE = "System reminder: File exists but has empty contents"
_READ_FAILED = "LangSmith read failed for %s: %s"
_TEXT_NOT_UTF8 = "Text-extension file %s contained invalid UTF-8; returning as base64"
_UPLOAD_FAILED = "Failed to upload %s: %s"
_DIRECTORY_MARK = "is a directory"
_CODE_INVALID = "invalid_path"
_CODE_MISSING = "file_not_found"
_CODE_DIRECTORY = "is_directory"
_CODE_DENIED = "permission_denied"
_TEXT = "text"
_AS_BASE64 = "base64"
_AS_TEXT = "utf-8"


def _chosen_timeout(explicit, fallback):
    """只有 ``None`` 才换默认值。``0`` 和 ``False`` 都原样留下。"""
    if explicit is None:
        return fallback
    return explicit


def _merge_streams(stdout, stderr) -> str:
    """假值 stdout 先变成空串。假值 stderr 不参与拼接。

    stderr 为真时做 ``output += ("\\n" + stderr if output else stderr)``。
    空串这一支是 ``"" + stderr``，不把 stderr 原对象直接交回去。
    """
    output = stdout or ""
    if stderr:
        output += ("\n" + stderr if output else stderr)
    return output


def _response_from_run(completed) -> ExecuteResponse:
    return ExecuteResponse(
        output=_merge_streams(completed.stdout, completed.stderr),
        exit_code=completed.exit_code,
        truncated=False,
    )


def _binary_preview(file_path: str, payload: bytes) -> ReadResult:
    if len(payload) > MAX_BINARY_BYTES:
        message = f"File '{file_path}': Binary file exceeds maximum preview size of {MAX_BINARY_BYTES} bytes"
        return ReadResult(error=message)
    encoded = base64.b64encode(payload).decode("ascii")
    data = FileData(content=encoded, encoding=_AS_BASE64)
    return ReadResult(file_data=data)


def _empty_file() -> ReadResult:
    data = FileData(content=_EMPTY_FILE_NOTE, encoding=_AS_TEXT)
    return ReadResult(file_data=data)


def _blank_window() -> ReadResult:
    data = FileData(content="", encoding=_AS_TEXT)
    return ReadResult(file_data=data, no_lines_requested=True)


def _drop_one_trailing_blank(rows: list[str]) -> None:
    if rows and rows[-1] == "":
        rows.pop()


def _clip_page(content: str, returned_lines: int) -> tuple[str, int]:
    encoded = content.encode("utf-8")
    room = MAX_OUTPUT_BYTES - len(TRUNCATION_MSG.encode("utf-8"))
    if len(encoded) <= room:
        return content, returned_lines
    clipped = encoded[:room].decode("utf-8", errors="ignore")
    kept = clipped.count("\n") or 1
    return clipped + TRUNCATION_MSG, kept


def _paginate(file_path: str, text: str, offset, limit) -> ReadResult:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    rows = normalized.split("\n")
    _drop_one_trailing_blank(rows)
    offset, limit = normalize_read_bounds(offset, limit)
    if limit <= 0:
        return _blank_window()
    total_lines = len(rows)
    if not rows or offset >= total_lines:
        message = f"File '{file_path}': Line offset {offset} exceeds file length ({total_lines} lines)"
        return ReadResult(error=message)
    page = rows[offset : offset + limit]
    content, returned_lines = _clip_page("\n".join(page), len(page))
    end_line = offset + returned_lines
    next_offset = end_line if end_line < total_lines else None
    data = FileData(content=content, encoding=_AS_TEXT)
    return ReadResult(
        file_data=data,
        total_lines=total_lines,
        start_line=offset + 1,
        end_line=end_line,
        next_offset=next_offset,
    )


def _download_one(backend, path: str, missing_type, client_error):
    if not path.startswith("/"):
        return FileDownloadResponse(path=path, content=None, error=_CODE_INVALID)
    try:
        body = backend._sandbox.read(path)
    except missing_type:
        return FileDownloadResponse(path=path, content=None, error=_CODE_MISSING)
    except client_error as e:
        lowered = str(e).lower()
        code = _CODE_DIRECTORY if _DIRECTORY_MARK in lowered else _CODE_MISSING
        return FileDownloadResponse(path=path, content=None, error=code)
    return FileDownloadResponse(path=path, content=body, error=None)


def _upload_one(backend, path: str, content, client_error):
    if not path.startswith("/"):
        return FileUploadResponse(path=path, error=_CODE_INVALID)
    try:
        backend._sandbox.write(path, content)
    except client_error as e:
        logger.debug(_UPLOAD_FAILED, path, e)
        return FileUploadResponse(path=path, error=_CODE_DENIED)
    return FileUploadResponse(path=path, error=None)


class LangSmithSandbox(BaseSandbox):
    """已有 Sandbox 的薄包装。``enable_capture_offload`` 打开。"""

    enable_capture_offload = True

    def __init__(self, sandbox):
        self._async_client = None
        self._async_sandbox = None
        self._sandbox = sandbox
        self._default_timeout = 30 * 60

    @property
    def id(self):
        return self._sandbox.name

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        limit_seconds = _chosen_timeout(timeout, self._default_timeout)
        completed = self._sandbox.run(command, timeout=limit_seconds)
        return _response_from_run(completed)

    def _async_box(self):
        cached = self._async_sandbox
        if cached is not None:
            return cached
        fresh_client = self._sandbox._client.to_async()
        self._async_client = fresh_client
        built = self._sandbox.to_async(client=fresh_client)
        self._async_sandbox = built
        return built

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        limit_seconds = _chosen_timeout(timeout, self._default_timeout)
        box = self._async_box()
        completed = await box.run(command, timeout=limit_seconds)
        return _response_from_run(completed)

    async def aclose(self) -> None:
        client = self._async_client
        self._async_client = None
        self._async_sandbox = None
        if client is None:
            return
        await client.aclose()

    def write(self, file_path: str, content: str) -> WriteResult:
        from langsmith.sandbox import SandboxClientError

        gate = self._write_preflight(file_path)
        if gate is not None:
            return gate
        try:
            self._sandbox.write(file_path, content.encode("utf-8"))
        except SandboxClientError as e:
            message = f"Failed to write file '{file_path}': {e}"
            return WriteResult(error=message)
        return WriteResult(path=file_path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        from langsmith.sandbox import ResourceNotFoundError, SandboxClientError

        try:
            payload = self._sandbox.read(file_path)
        except ResourceNotFoundError:
            missing = f"File '{file_path}': file_not_found"
            return ReadResult(error=missing)
        except SandboxClientError as e:
            logger.warning(_READ_FAILED, file_path, e)
            notice = f"File '{file_path}': {type(e).__name__}: {e}"
            return ReadResult(error=notice)

        if not payload:
            return _empty_file()
        if _file_kind(file_path) != _TEXT:
            return _binary_preview(file_path, payload)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            logger.info(_TEXT_NOT_UTF8, file_path)
            return _binary_preview(file_path, payload)
        return _paginate(file_path, text, offset, limit)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        from langsmith.sandbox import ResourceNotFoundError, SandboxClientError

        packed: list[FileDownloadResponse] = []
        for path in paths:
            packed.append(_download_one(self, path, ResourceNotFoundError, SandboxClientError))
        return packed

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        from langsmith.sandbox import SandboxClientError

        packed: list[FileUploadResponse] = []
        for path, content in files:
            packed.append(_upload_one(self, path, content, SandboxClientError))
        return packed
