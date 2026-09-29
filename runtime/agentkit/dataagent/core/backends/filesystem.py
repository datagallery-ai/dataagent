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
"""把文件操作落到宿主机的普通文件系统上。

虚拟模式只改路径字符串怎么拼到 ``root_dir`` 下面，没有进程隔离，也不是沙箱。
默认 ``virtual_mode=True``。实例上只留 ``cwd``、``virtual_mode``、``max_file_size_bytes``。

几条和「看起来更安全」相反、但必须留着的行为：

- 虚拟路径先补前导 ``/``，再查 ``~``。补完之后一定以 ``/`` 开头，``startswith("~")``
  够不着 ``~/secret``，那个名字是字面目录。
- ``".." in`` 是子串。``foo..bar`` 也会被拒绝。
- 非虚拟的绝对路径不 ``resolve()``，符号链接保持原样。相对路径会解析，``..`` 可以离开根。
- ``read`` 找不到文件时没有 ``Error: `` 前缀。``edit`` 有 ``Error: File``。
  ``delete`` 有 ``Error: `` 但没有单词 ``File``。
- ``delete`` 删的是 ``_resolve_path`` 交回来的那个路径。虚拟模式和相对路径往往已经
  跟随过链接，删的是目标；非虚拟绝对路径仍是链接本身。
- 没有 ripgrep 时，对单个文件的 Python 搜索扫的是父目录，兄弟文件的命中也会出现。
  ripgrep 对单文件只把那个文件放进 argv。
- ``glob`` 的 path 里出现 ``..`` 会抛 ``ValueError``；pattern 里出现 ``..`` 是
  ``matches=None`` 加错误文案。
"""

import asyncio
import base64
import bisect
import datetime
import errno
import functools
import json
import logging
import os
import pathlib
import shutil
import subprocess
import threading
import time

from . import protocol as contracts
from . import utils as shared

logger = logging.getLogger(__name__)

# 深替换探针核对的是本模块上的 ReadResult 与 protocol.ReadResult 是不是同一个对象。
ReadResult = contracts.ReadResult

# 这三个名字测试会在 import 之后打补丁，调用时再读，不能写进默认参数。
MAX_VIDEO_INPUT_BYTES = shared.MAX_VIDEO_INPUT_BYTES
DEFAULT_GREP_TIMEOUT = contracts.DEFAULT_GREP_TIMEOUT
ASYNC_GREP_TIMEOUT = contracts.ASYNC_GREP_TIMEOUT
compile_grep_include_glob = shared.compile_grep_include_glob
InvalidGlobPatternError = shared.InvalidGlobPatternError
_get_backend_read_file_type = shared._get_backend_read_file_type
check_empty_content = shared.check_empty_content
slice_read_response = shared.slice_read_response
perform_string_replacement = shared.perform_string_replacement

_DEFAULT_GLOB_TIMEOUT = 5
_RIPGREP_STDERR_CAPTURE_LIMIT = 500
_RIPGREP_STDERR_READ_SIZE = 8192
_WIN32_ERROR_CANT_RESOLVE_FILENAME = 1921
_LINE_CLOCK_STRIDE = 2048

Path = pathlib.Path


def _is_eloop_oserror(exc: BaseException | None) -> bool:
    """``ELOOP`` 或 Windows 的 1921。普通 ``OSError`` 不是。"""
    if not isinstance(exc, OSError):
        return False
    if exc.errno == errno.ELOOP:
        return True
    return getattr(exc, "winerror", None) == _WIN32_ERROR_CANT_RESOLVE_FILENAME


def _is_symlink_loop_error(exc: Exception) -> bool:
    """自己是循环，或者被包了一层 ``RuntimeError``（只看直接的 cause / context）。"""
    if _is_eloop_oserror(exc):
        return True
    if not isinstance(exc, RuntimeError):
        return False
    return _is_eloop_oserror(exc.__cause__) or _is_eloop_oserror(exc.__context__)


def _raise_if_symlink_loop(path: Path) -> None:
    """``resolve()`` 在 3.13 上不再为循环抛错，链接本身再用 ``stat`` 探一次。

    先问 ``is_symlink()``。3.12 上这条会把穿过自环、但自己不是那条链接的路径上的
    ``ELOOP`` 吞成 False；那种路径必须原样返回，留给后面的 ``exists`` / ``open``。
    直接 ``stat`` 会把 errno 40 重新抛出去。不是链接就到此为止，缺文件的失败也留着。
    """
    if not path.is_symlink():
        return
    try:
        path.stat()
    except OSError as exc:
        if _is_eloop_oserror(exc):
            raise


def _map_exception_to_standard_error(exc: Exception) -> contracts.FileOperationError | None:
    """上传 / 下载把异常收成四个错误码。收不进的返回 ``None``，调用方要重新抛出。"""
    if isinstance(exc, FileNotFoundError):
        return contracts.FILE_NOT_FOUND
    if _is_symlink_loop_error(exc):
        return contracts.INVALID_PATH
    if isinstance(exc, PermissionError):
        return contracts.PERMISSION_DENIED
    if isinstance(exc, IsADirectoryError):
        return contracts.IS_DIRECTORY
    if isinstance(exc, (NotADirectoryError, FileExistsError, ValueError)):
        return contracts.INVALID_PATH
    return None


def _failure_label(exc: BaseException, *, virtual: bool) -> str:
    """给模型看的异常摘要。虚拟模式不用 ``str(exc)``，那里面常常嵌着真实根目录。"""
    kind = type(exc).__name__
    if isinstance(exc, OSError):
        detail = exc.strerror or ""
    else:
        reason = getattr(exc, "reason", None)
        if reason:
            detail = str(reason)
        elif virtual:
            detail = ""
        else:
            detail = str(exc)
    if not detail:
        return kind
    return f"{kind}: {detail}"


def _describe(shown: str, node: Path, *, directory: bool) -> contracts.FileInfo:
    """``ls`` / ``glob`` 的一条。``stat`` 失败时只留路径和 ``is_dir``。"""
    info: contracts.FileInfo = {"path": shown, "is_dir": directory}
    try:
        st = node.stat()
    except OSError:
        return info
    info["size"] = 0 if directory else int(st.st_size)
    info["modified_at"] = datetime.datetime.fromtimestamp(st.st_mtime).isoformat()
    return info


def _by_path(info: contracts.FileInfo) -> str:
    return info.get("path", "")


def _summarize_unreadable(problems: list[str]) -> str | None:
    if not problems:
        return None
    return "One or more files could not be fully searched:\n" + "\n".join(problems)


def _caller_label(path: str | None) -> str:
    """``glob`` 的日志和错误用调用方原串。没传 path 时是字面量 ``<default>``。"""
    if path is None:
        return "<default>"
    return path


@functools.cache
def _resolve_ripgrep_path() -> str | None:
    located = shutil.which("rg")
    if located is None:
        logger.info(
            "ripgrep ('rg') not found on PATH; using Python grep fallback. Install ripgrep for faster searches and automatic .gitignore handling."
        )
    return located


def _normalize_newlines(text: str) -> str:
    """先 ``\\r\\n`` 再单独的 ``\\r``。顺序反了会把一对 CRLF 变成两个换行。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _strip_record(raw_line: str) -> str:
    """``newline="\\n"`` 时 CRLF 还在行尾；单独的 ``\\r`` 不当换行，留在文本里。"""
    if raw_line.endswith("\r\n"):
        return raw_line[:-2]
    return raw_line.removesuffix("\n")


def _write_flags(*, create: bool) -> int:
    flags = os.O_WRONLY | os.O_TRUNC
    if create:
        flags |= os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _read_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


class FilesystemBackend(contracts.BackendProtocol):
    """宿主机文件系统后端。直接基类只有 ``BackendProtocol``，这样沙箱子类两条 MRO 都能线性化。

    八个 ``a*`` 方法不在这里覆写，走基类丢进线程的实现。只有 ``agrep`` 自己处理
    ``context_lines``。``max_file_size_mb`` 只影响 Python 搜索跳过过大的文件。
    """

    def __init__(self, root_dir: str | Path | None = None, virtual_mode: bool = True, max_file_size_mb: int = 10) -> None:
        self.cwd = Path(root_dir).resolve() if root_dir else Path.cwd()
        self.virtual_mode = virtual_mode
        self.max_file_size_bytes = max_file_size_mb * 1024 * 1024

    def _resolve_path(self, key: str) -> Path:
        """虚拟模式先补斜杠再做子串检查；非虚拟的绝对路径原样返回。"""
        if self.virtual_mode:
            anchored = key if key.startswith("/") else "/" + key
            # startswith("~") 在补过斜杠之后永远为假。不要把这行「修」成真的拒绝波浪号。
            if ".." in anchored or anchored.startswith("~"):
                raise ValueError("Path traversal not allowed")
            full = (self.cwd / anchored.lstrip("/")).resolve()
            try:
                full.relative_to(self.cwd)
            except ValueError:
                raise ValueError(f"Path:{full} outside root directory: {self.cwd}") from None
            _raise_if_symlink_loop(full)
            return full
        candidate = Path(key)
        if candidate.is_absolute():
            _raise_if_symlink_loop(candidate)
            return candidate
        resolved = (self.cwd / candidate).resolve()
        _raise_if_symlink_loop(resolved)
        return resolved

    def _to_virtual_path(self, path: Path) -> str:
        """``cwd`` 自己得到 ``/.``。不在根下时让 ``relative_to`` 的 ``ValueError`` 原样出去。"""
        resolved = path.resolve()
        return "/" + resolved.relative_to(self.cwd).as_posix()

    def _display_path(self, path: Path) -> str:
        """错误文案用的路径。虚拟模式转换失败时只留文件名，避免把根目录漏出去。"""
        if not self.virtual_mode:
            return str(path)
        try:
            return self._to_virtual_path(path)
        except (ValueError, OSError, RuntimeError):
            return path.name or "/"

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[contracts.FileUploadResponse]:
        """逐条上传。映射不了的异常中断整批，前面已经成功的响应也不返回。

        目标已经是目录时不做 ``is_dir()`` 预检，让 ``open`` 自己失败。
        """
        responses: list[contracts.FileUploadResponse] = []
        for path, content in files:
            try:
                resolved = self._resolve_path(path)
                resolved.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(resolved, _write_flags(create=True), 0o644)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                responses.append(contracts.FileUploadResponse(path=path, error=None))
            except Exception as exc:
                code = _map_exception_to_standard_error(exc)
                if code is None:
                    raise
                responses.append(contracts.FileUploadResponse(path=path, error=code))
        return responses

    def download_files(self, paths: list[str]) -> list[contracts.FileDownloadResponse]:
        """目录在 ``open`` 之前判断。映射不了的异常同样中断后面的路径。"""
        responses: list[contracts.FileDownloadResponse] = []
        for path in paths:
            try:
                responses.append(self._download_one(path))
            except Exception as exc:
                code = _map_exception_to_standard_error(exc)
                if code is None:
                    raise
                responses.append(contracts.FileDownloadResponse(path=path, content=None, error=code))
        return responses

    def _download_one(self, path: str) -> contracts.FileDownloadResponse:
        resolved = self._resolve_path(path)
        if resolved.is_dir():
            return contracts.FileDownloadResponse(path=path, content=None, error=contracts.IS_DIRECTORY)
        fd = os.open(resolved, _read_flags())
        try:
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                payload = handle.read()
        finally:
            if fd >= 0:
                os.close(fd)
        return contracts.FileDownloadResponse(path=path, content=payload, error=None)

    def delete(self, file_path: str) -> contracts.DeleteResult:
        """删 ``_resolve_path`` 的结果。链接只有在返回值仍是链接时才只 ``unlink`` 自己。"""
        try:
            resolved = self._resolve_path(file_path)
        except (OSError, RuntimeError) as exc:
            return contracts.DeleteResult(error=f"Error deleting '{file_path}': {exc}")
        try:
            if not resolved.exists() and not resolved.is_symlink():
                return contracts.DeleteResult(error=f"Error: '{file_path}' not found")
            if resolved.is_symlink():
                resolved.unlink()
            elif resolved.is_dir():
                shutil.rmtree(resolved)
            else:
                resolved.unlink()
            return contracts.DeleteResult(path=file_path)
        except (OSError, RuntimeError) as exc:
            return contracts.DeleteResult(error=f"Error deleting '{file_path}': {exc}")

    def write(self, file_path: str, content: str) -> contracts.WriteResult:
        """父目录会建出来。``newline=""`` 让调用方的 ``\\n`` 原样落盘。成功时 ``path`` 是入参。"""
        try:
            resolved = self._resolve_path(file_path)
        except (OSError, RuntimeError) as exc:
            return contracts.WriteResult(error=f"Error writing file '{file_path}': {exc}")
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(resolved, _write_flags(create=True), 0o644)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            return contracts.WriteResult(path=file_path)
        except (OSError, UnicodeEncodeError) as exc:
            return contracts.WriteResult(error=f"Error writing file '{file_path}': {exc}")

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> contracts.EditResult:
        """先读全文再替换。失败文案原样返回，不写盘。写回没有 ``O_CREAT``。"""
        try:
            resolved = self._resolve_path(file_path)
        except (OSError, RuntimeError) as exc:
            return contracts.EditResult(error=f"Error editing file '{file_path}': {exc}")
        try:
            if not resolved.exists() or not resolved.is_file():
                return contracts.EditResult(error=f"Error: File '{file_path}' not found")
            content = self._read_text(resolved)
            old_string = _normalize_newlines(old_string)
            new_string = _normalize_newlines(new_string)
            replaced = perform_string_replacement(content, old_string, new_string, replace_all)
            if isinstance(replaced, str):
                return contracts.EditResult(error=replaced)
            new_content, occurrences = replaced
            fd = os.open(resolved, _write_flags(create=False))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(new_content)
            return contracts.EditResult(path=file_path, occurrences=int(occurrences))
        except (OSError, UnicodeDecodeError, UnicodeEncodeError) as exc:
            return contracts.EditResult(error=f"Error editing file '{file_path}': {exc}")

    def _read_text(self, resolved: Path) -> str:
        fd = os.open(resolved, _read_flags())
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                return handle.read()
        finally:
            if fd >= 0:
                os.close(fd)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> contracts.ReadResult:
        """先确认是文件，再按调用方路径的后缀分类。文本分页交给 ``slice_read_response``。

        找不到时的句子没有 ``Error: `` 前缀。视频体积读的是模块全局，超了就不把字节读进来。
        """
        try:
            resolved = self._resolve_path(file_path)
        except (OSError, RuntimeError) as exc:
            return contracts.ReadResult(error=f"Error reading file '{file_path}': {exc}")
        try:
            if not resolved.exists() or not resolved.is_file():
                return contracts.ReadResult(error=f"File '{file_path}' not found")
            return self._read_open(file_path, resolved, offset, limit)
        except (OSError, UnicodeDecodeError) as exc:
            return contracts.ReadResult(error=f"Error reading file '{file_path}': {exc}")

    def _read_open(self, file_path: str, resolved: Path, offset: int, limit: int) -> contracts.ReadResult:
        fd = os.open(resolved, _read_flags())
        try:
            kind = _get_backend_read_file_type(file_path)
            if kind != "text":
                if kind == "video" and os.fstat(fd).st_size > MAX_VIDEO_INPUT_BYTES:
                    return contracts.ReadResult(
                        error=f"Video file exceeds maximum input size of {MAX_VIDEO_INPUT_BYTES} bytes"
                    )
                with os.fdopen(fd, "rb") as raw:
                    fd = -1
                    payload = raw.read()
                encoded = base64.standard_b64encode(payload).decode("ascii")
                data = contracts.FileData(content=encoded, encoding="base64")
            else:
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = -1
                    content = handle.read()
                reminder = check_empty_content(content)
                if reminder:
                    data = contracts.FileData(content=reminder, encoding="utf-8")
                else:
                    return slice_read_response(contracts.FileData(content=content, encoding="utf-8"), offset, limit)
        finally:
            if fd >= 0:
                os.close(fd)
        return contracts.ReadResult(file_data=data)

    def ls(self, path: str) -> contracts.LsResult:
        """只列直接子项。``ValueError``（路径穿越）不在这里接。中止时已经收进来的条目仍返回。"""
        try:
            directory = self._resolve_path(path)
            if not directory.exists():
                return contracts.LsResult(error=f"Path '{path}': path_not_found", entries=None)
            if not directory.is_dir():
                return contracts.LsResult(error=f"Path '{path}': not_a_directory", entries=None)
        except (OSError, RuntimeError) as exc:
            return contracts.LsResult(error=f"Cannot list '{path}': {exc}", entries=None)
        rows: list[contracts.FileInfo] = []
        problems: list[str] = []
        try:
            for child in directory.iterdir():
                row, problem = self._ls_child(child)
                if problem:
                    problems.append(problem)
                elif row is not None:
                    rows.append(row)
        except (OSError, RuntimeError) as exc:
            notice = f"Listing of '{path}' aborted: {exc}"
            logger.warning("%s", notice)
            problems.append(notice)
        rows.sort(key=_by_path)
        error = "\n".join(sorted(problems)) if problems else None
        return contracts.LsResult(error=error, entries=rows)

    def _ls_child(self, child: Path) -> tuple[contracts.FileInfo | None, str | None]:
        try:
            file_like = child.is_file()
            dir_like = child.is_dir()
        except (OSError, RuntimeError) as exc:
            notice = f"child error: cannot stat '{child}': {exc}"
            logger.warning("%s", notice)
            return None, notice
        if not file_like and not dir_like:
            return None, self._ls_non_regular(child)
        shown, problem = self._ls_name(child, dir_like)
        if problem or shown is None:
            return None, problem
        return _describe(shown, child, directory=dir_like), None

    def _ls_non_regular(self, child: Path) -> str | None:
        """既不是文件也不是目录。链接解析失败记一条错误；无论成败都不列进去。"""
        try:
            if child.is_symlink():
                landed = child.resolve()
                _raise_if_symlink_loop(landed)
        except (OSError, RuntimeError, ValueError) as exc:
            notice = f"child error: cannot resolve '{child}': {exc}"
            logger.warning("%s", notice)
            return notice
        return None

    def _ls_name(self, child: Path, dir_like: bool) -> tuple[str | None, str | None]:
        if self.virtual_mode:
            try:
                shown = self._to_virtual_path(child)
            except ValueError:
                logger.debug("ls 跳过落在根外的条目 %s", child.name)
                return None, None
            except (OSError, RuntimeError) as exc:
                notice = f"child error: cannot resolve '{child}': {exc}"
                logger.warning("%s", notice)
                return None, notice
        else:
            shown = str(child)
        if dir_like:
            shown = shown + "/"
        return shown, None

    def glob(self, pattern: str, path: str | None = None) -> contracts.GlobResult:
        """``None`` 和 ``/`` 直接用 ``cwd``，不经过 ``_resolve_path``。空串会经过。

        预算用尽是 ``truncated`` 加 ``budget``，遍历中途异常是 ``aborted partway`` 且不标截断。
        """
        try:
            matcher = compile_grep_include_glob(pattern)
        except InvalidGlobPatternError as exc:
            return contracts.GlobResult(error=str(exc), matches=None)
        display = _caller_label(path)
        try:
            search = self._glob_root(path)
            if not search.exists() or not search.is_dir():
                return contracts.GlobResult(matches=[])
        except (OSError, RuntimeError) as exc:
            return contracts.GlobResult(error=f"Error globbing path '{display}': {exc}", matches=[])
        rows, truncated, aborted = self._collect_glob(search, matcher, display)
        rows.sort(key=_by_path)
        if aborted is not None:
            return contracts.GlobResult(error=aborted, matches=rows, truncated=False, truncation_reason=None)
        reason = "budget" if truncated else None
        return contracts.GlobResult(matches=rows, truncated=truncated, truncation_reason=reason)

    def _glob_root(self, path: str | None) -> Path:
        if path is None or path == "/":
            return self.cwd
        return self._resolve_path(path)

    def _collect_glob(self, search: Path, matcher, display: str) -> tuple[list[contracts.FileInfo], bool, str | None]:
        budget = _DEFAULT_GLOB_TIMEOUT
        deadline = time.monotonic() + budget
        rows: list[contracts.FileInfo] = []
        try:
            for matched in search.rglob("*"):
                if time.monotonic() > deadline:
                    notice = f"Glob of '{display}' timed out after {budget}s with {len(rows)} match(es); returning partial results"
                    logger.warning("%s", notice)
                    return rows, True, None
                row = self._glob_row(search, matched, matcher)
                if row is not None:
                    rows.append(row)
        except (OSError, RuntimeError, ValueError) as exc:
            notice = f"Glob of '{display}' aborted partway: {exc}"
            logger.warning("%s", notice, exc_info=True)
            return rows, False, notice
        return rows, False, None

    def _glob_row(self, search: Path, matched: Path, matcher) -> contracts.FileInfo | None:
        try:
            relative = matched.relative_to(search).as_posix()
        except ValueError:
            return None
        if not matcher(relative):
            return None
        try:
            if not matched.is_file():
                return None
        except (OSError, RuntimeError):
            return None
        shown = self._glob_shown(matched)
        if shown is None:
            return None
        return _describe(shown, matched, directory=False)

    def _glob_shown(self, matched: Path) -> str | None:
        if not self.virtual_mode:
            return str(matched)
        try:
            matched.resolve().relative_to(self.cwd)
        except (ValueError, OSError, RuntimeError):
            return None
        try:
            return self._to_virtual_path(matched)
        except ValueError:
            logger.debug("glob 跳过 %s", matched.name)
            return None
        except (OSError, RuntimeError):
            logger.warning("glob 无法映射 %s", matched.name, exc_info=True)
            return None

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> contracts.GrepResult:
        """字面量子串。``path`` 缺省时解析 ``.``，不是 ``/``。路径上的 ``ValueError`` 变成空结果。

        命中按发现顺序铺开，不再排序。``context_lines=0`` 不挂上下文字段。
        """
        if context_lines < 0:
            raise ValueError("context_lines must be non-negative")
        refusal = self._refused_grep_glob_error(glob)
        if refusal is not None:
            return contracts.GrepResult(error=refusal, matches=[])
        try:
            base_full = self._resolve_path(path or ".")
        except ValueError:
            return contracts.GrepResult(matches=[])
        except (OSError, RuntimeError) as exc:
            return contracts.GrepResult(error=f"Error searching path '{path or '.'}': {exc}", matches=[])
        try:
            if not base_full.exists():
                return contracts.GrepResult(matches=[])
        except OSError as exc:
            return contracts.GrepResult(error=f"Error searching path '{path or '.'}': {exc}", matches=[])
        found, truncated = self._ripgrep_search(pattern, base_full, glob, max_count)
        context_newline: str | None = "\n"
        partial_error: str | None = None
        if found is None:
            found, truncated, partial_error = self._python_search(pattern, base_full, glob, max_count=max_count)
            context_newline = None
        matches: list[contracts.GrepMatch] = []
        for file_path, pairs in found.items():
            for line_no, text in pairs:
                matches.append({"path": file_path, "line": line_no, "text": text})
        if context_lines:
            partial_error = self._apply_grep_context(
                matches, context_lines, partial_error, pattern, newline=context_newline
            )
        return contracts.GrepResult(error=partial_error, matches=matches, truncated=truncated)

    @staticmethod
    def _refused_grep_glob_error(glob: str | None) -> str | None:
        if glob is None:
            return None
        try:
            compile_grep_include_glob(glob)
        except InvalidGlobPatternError as exc:
            return str(exc)
        return None

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> contracts.GrepResult:
        """``context_lines=0`` 走基类，不把这个参数传进去，也不自己套 ``wait_for``。

        大于 0 时用本模块的 ``ASYNC_GREP_TIMEOUT``。超时只停止等待，不取消线程里的搜索。
        """
        if context_lines < 0:
            raise ValueError("context_lines must be non-negative")
        if context_lines == 0:
            return await super().agrep(pattern, path, glob, max_count=max_count)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.grep, pattern, path, glob, max_count=max_count, context_lines=context_lines),
                timeout=ASYNC_GREP_TIMEOUT,
            )
        except TimeoutError:
            logger.warning(
                "agrep timed out after %ds (pattern=%r, path=%r, glob=%r)",
                ASYNC_GREP_TIMEOUT,
                pattern, path, glob,
            )
            return contracts.GrepResult(
                error=f"Error: grep timed out after {ASYNC_GREP_TIMEOUT}s. Try a more specific pattern or a narrower path.",
            )

    def _ripgrep_search(
        self, pattern: str, base_full: Path, include_glob: str | None, max_count: int | None = None
    ) -> tuple[dict[str, list[tuple[int, str]]] | None, bool]:
        rg_path = _resolve_ripgrep_path()
        if rg_path is None:
            return None, False
        command = [rg_path, "--json", "-F"]
        if max_count is not None:
            command += ["-m", str(max_count + 1)]
        if include_glob:
            command += ["--glob", include_glob]
        if base_full.is_dir():
            command += ["--", pattern, "."]
            workdir: str | None = str(base_full)
        else:
            command += ["--", pattern, str(base_full)]
            workdir = None
        try:
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=workdir
            )
        except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
            logger.warning(
                "ripgrep subprocess failed (%s: %s); using Python grep fallback",
                type(exc).__name__,
                exc,
            )
            _resolve_ripgrep_path.cache_clear()
            return None, False
        return self._consume_ripgrep(proc, base_full, max_count)

    def _consume_ripgrep(
        self, proc, base_full: Path, max_count: int | None
    ) -> tuple[dict[str, list[tuple[int, str]]] | None, bool]:
        chunks: list[str] = []
        drainer = threading.Thread(
            target=FilesystemBackend._drain_ripgrep_stderr, args=(proc, chunks), daemon=True
        )
        drainer.start()
        timed_out = threading.Event()

        def _expire() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(DEFAULT_GREP_TIMEOUT, _expire)
        timer.daemon = True
        timer.start()
        found: dict[str, list[tuple[int, str]]] = {}
        truncated = False
        kept = 0
        base_resolved = base_full.resolve()
        try:
            stdout = proc.stdout
            if stdout is not None:
                for line in stdout:
                    hit = self._take_rg_hit(line, base_full, base_resolved)
                    if hit is None:
                        continue
                    shown, line_no, text = hit
                    if max_count is not None and kept >= max_count:
                        truncated = True
                        proc.terminate()
                        break
                    found.setdefault(shown, []).append((line_no, text))
                    kept += 1
        finally:
            timer.cancel()
            self._reap_ripgrep(proc)
            drainer.join()
        stderr_text = "".join(chunks).strip()[:500]
        if timed_out.is_set() and found:
            logger.warning("ripgrep timed out after %ds; returning partial results", DEFAULT_GREP_TIMEOUT)
            return found, True
        if timed_out.is_set():
            logger.warning(
                "ripgrep timed out after %ds with no output; using Python grep fallback", DEFAULT_GREP_TIMEOUT
            )
            return None, False
        if truncated:
            return found, True
        code = proc.returncode
        if code not in (0, 1):
            logger.warning("ripgrep exited %d (stderr=%r); using Python grep fallback", code, stderr_text)
            return None, False
        return found, truncated

    def _take_rg_hit(self, line: str, base_full: Path, base_resolved: Path) -> tuple[str, int, str] | None:
        parsed = _parse_rg_payload(line)
        if parsed is None:
            return None
        raw_path, line_no, text = parsed
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = base_full / raw_path
        try:
            candidate.resolve().relative_to(base_resolved)
        except (ValueError, OSError):
            logger.warning("Skipping ripgrep result outside search root: path=%s root=%s", candidate, base_full)
            return None
        if not self.virtual_mode:
            return str(candidate), line_no, text
        try:
            return self._to_virtual_path(candidate), line_no, text
        except ValueError:
            logger.debug("丢掉根外的 ripgrep 路径 %s", candidate)
            return None
        except (OSError, RuntimeError):
            logger.warning("ripgrep 路径无法映射 %s", candidate, exc_info=True)
            return None

    @staticmethod
    def _drain_ripgrep_stderr(proc: "subprocess.Popen[str]", chunks: list[str]) -> None:
        """保留前 500 个字符，管道里剩下的继续读完，避免子进程堵死。"""
        stream = proc.stderr
        if stream is None:
            return
        kept = 0
        try:
            while True:
                piece = stream.read(_RIPGREP_STDERR_READ_SIZE)
                if not piece:
                    break
                if kept >= _RIPGREP_STDERR_CAPTURE_LIMIT:
                    continue
                room = _RIPGREP_STDERR_CAPTURE_LIMIT - kept
                chunks.append(piece[:room])
                kept += len(piece[:room])
        except (OSError, ValueError):
            logger.debug("ripgrep stderr 读取中断", exc_info=True)

    @staticmethod
    def _reap_ripgrep(proc: "subprocess.Popen[str]") -> None:
        stdout = proc.stdout
        if stdout is not None:
            try:
                stdout.close()
            except OSError:
                logger.debug("关闭 ripgrep stdout 失败", exc_info=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("ripgrep did not exit after SIGKILL; abandoning process handle")

    def _python_search(
        self,
        pattern: str,
        base_full: Path,
        include_glob: str | None,
        *,
        max_count: int | None = None,
        timeout: int = DEFAULT_GREP_TIMEOUT,
    ) -> tuple[dict[str, list[tuple[int, str]]], bool, str | None]:
        """文件路径时扫父目录，不过滤回那一个文件。行内时钟每 2048 行才看一次。"""
        scan_root = base_full if base_full.is_dir() else base_full.parent
        deadline = time.monotonic() + timeout
        matcher = compile_grep_include_glob(include_glob) if include_glob else None
        found: dict[str, list[tuple[int, str]]] = {}
        problems: list[str] = []
        kept = 0
        try:
            for entry in scan_root.rglob("*"):
                if time.monotonic() > deadline:
                    return found, True, self._note_grep_timeout(base_full, timeout, len(found))
                if not _regular_file(entry):
                    continue
                if matcher is not None and not _glob_allows(matcher, entry, scan_root):
                    continue
                if not self._within_size(entry):
                    continue
                shown = self._search_name(entry)
                if shown is None:
                    continue
                outcome = self._scan_lines(entry, shown, pattern, deadline, timeout, base_full, found, problems, kept, max_count)
                if outcome is None:
                    kept = sum(len(items) for items in found.values())
                    continue
                return outcome
        except (OSError, RuntimeError) as exc:
            label = _failure_label(exc, virtual=self.virtual_mode)
            notice = f"Grep of '{self._display_path(base_full)}' aborted after {len(found)} matching file(s): {label}"
            logger.warning("%s", notice, exc_info=True)
            return found, False, notice
        return found, False, _summarize_unreadable(problems)

    def _note_grep_timeout(self, base_full: Path, timeout: float, matched_files: int) -> None:
        notice = f"Grep of '{self._display_path(base_full)}' timed out after {timeout}s with {matched_files} matching file(s); returning partial results"
        logger.warning("%s", notice)
        return None

    def _within_size(self, entry: Path) -> bool:
        try:
            size = entry.stat().st_size
        except OSError:
            return False
        return size <= self.max_file_size_bytes

    def _search_name(self, entry: Path) -> str | None:
        if not self.virtual_mode:
            return str(entry)
        try:
            return self._to_virtual_path(entry)
        except ValueError:
            logger.debug("搜索跳过 %s", entry.name)
            return None
        except (OSError, RuntimeError):
            logger.warning("搜索无法映射 %s", entry.name, exc_info=True)
            return None

    def _scan_lines(
        self,
        entry: Path,
        shown: str,
        pattern: str,
        deadline: float,
        timeout: float,
        base_full: Path,
        found: dict[str, list[tuple[int, str]]],
        problems: list[str],
        kept: int,
        max_count: int | None,
    ) -> tuple[dict[str, list[tuple[int, str]]], bool, str | None] | None:
        saw_text = False
        line_number = 0
        try:
            with entry.open(encoding="utf-8", errors="strict") as handle:
                for raw_line in handle:
                    line_number += 1
                    saw_text = True
                    if line_number % _LINE_CLOCK_STRIDE == 0 and time.monotonic() > deadline:
                        self._note_grep_timeout(base_full, timeout, len(found))
                        return found, True, None
                    if pattern not in raw_line:
                        continue
                    if max_count is not None and kept >= max_count:
                        return found, True, _summarize_unreadable(problems)
                    found.setdefault(shown, []).append((line_number, raw_line.rstrip("\n")))
                    kept += 1
        except UnicodeDecodeError as exc:
            if saw_text or shown in found:
                problems.append(f"- {shown}: {_failure_label(exc, virtual=self.virtual_mode)}")
            else:
                logger.debug("按二进制跳过 %s", shown, exc_info=True)
        except (OSError, RuntimeError) as exc:
            problems.append(f"- {shown}: {_failure_label(exc, virtual=self.virtual_mode)}")
        return None

    @staticmethod
    def _grep_context_ranges(file_matches: list[contracts.GrepMatch], context_lines: int) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for line in sorted(match["line"] for match in file_matches):
            start = max(1, line - context_lines)
            end = line + context_lines
            if spans and start <= spans[-1][1] + 1:
                spans[-1] = (spans[-1][0], max(spans[-1][1], end))
            else:
                spans.append((start, end))
        return spans

    def _read_grep_context(
        self, file_path: str, line_ranges: list[tuple[int, int]], *, newline: str | None = None
    ) -> tuple[dict[int, str], bool]:
        try:
            resolved = self._resolve_path(file_path)
            collected: dict[int, str] = {}
            with resolved.open(encoding="utf-8", errors="strict", newline=newline) as handle:
                if not line_ranges:
                    handle.read()
                    return collected, True
                last = line_ranges[-1][1]
                for index, raw_line in enumerate(handle, start=1):
                    if index > last:
                        break
                    collected[index] = _strip_record(raw_line)
            return collected, True
        except (OSError, RuntimeError, UnicodeDecodeError, ValueError) as exc:
            logger.debug("Could not read grep context for %s: %s", file_path, exc)
            return {}, False

    def _add_grep_context(
        self, matches: list[contracts.GrepMatch], context_lines: int, pattern: str, *, newline: str | None
    ) -> list[str]:
        groups: dict[str, list[contracts.GrepMatch]] = {}
        order: list[str] = []
        for match in matches:
            key = match["path"]
            bucket = groups.get(key)
            if bucket is None:
                bucket = []
                groups[key] = bucket
                order.append(key)
            bucket.append(match)
        failed: list[str] = []
        for key in order:
            group = groups[key]
            lines, ok = self._read_grep_context(key, self._grep_context_ranges(group, context_lines), newline=newline)
            if not ok:
                failed.append(key)
                for match in group:
                    match["context_before"] = []
                    match["context_after"] = []
                continue
            self._fill_context(group, lines, pattern, context_lines)
        return failed

    def _fill_context(
        self, group: list[contracts.GrepMatch], lines: dict[int, str], pattern: str, context_lines: int
    ) -> None:
        match_lines = {match["line"] for match in group}
        usable = [
            (line_no, text)
            for line_no, text in sorted(lines.items())
            if line_no not in match_lines and pattern not in text
        ]
        numbers = [line_no for line_no, _text in usable]
        for match in group:
            line = match["line"]
            low = max(1, line - context_lines)
            high = line + context_lines
            left = bisect.bisect_left(numbers, low)
            mid = bisect.bisect_left(numbers, line)
            after = bisect.bisect_right(numbers, line)
            right = bisect.bisect_right(numbers, high)
            match["context_before"] = [{"line": numbers[i], "text": usable[i][1]} for i in range(left, mid)]
            match["context_after"] = [{"line": numbers[i], "text": usable[i][1]} for i in range(after, right)]

    def _apply_grep_context(
        self,
        matches: list[contracts.GrepMatch],
        context_lines: int,
        partial_error: str | None,
        pattern: str,
        *,
        newline: str | None,
    ) -> str | None:
        failed = self._add_grep_context(matches, context_lines, pattern, newline=newline)
        if not failed:
            return partial_error
        joined = ", ".join(sorted(failed))
        context_error = f"Error: could not read context for {len(failed)} file(s) (non-UTF-8 or unreadable): {joined}"
        if partial_error:
            return f"{partial_error}\n{context_error}"
        return context_error


def _parse_rg_payload(line: str) -> tuple[str, int, str] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if kind == "error":
        logger.debug("ripgrep error 事件 %s", payload.get("data"))
        return None
    if kind != "match":
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    raw_path = _rg_path_text(data.get("path"))
    line_no = data.get("line_number")
    if raw_path is None or not isinstance(line_no, int) or isinstance(line_no, bool):
        return None
    lines_obj = data.get("lines")
    raw_text = ""
    if isinstance(lines_obj, dict):
        candidate = lines_obj.get("text")
        if isinstance(candidate, str):
            raw_text = candidate
    return raw_path, line_no, raw_text.rstrip("\n")


def _rg_path_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
    return None


def _regular_file(entry: Path) -> bool:
    try:
        return entry.is_file()
    except (OSError, RuntimeError):
        return False


def _glob_allows(matcher, entry: Path, scan_root: Path) -> bool:
    try:
        relative = entry.relative_to(scan_root).as_posix()
    except ValueError:
        return False
    try:
        return bool(matcher(relative))
    except (OSError, RuntimeError, ValueError):
        return False
