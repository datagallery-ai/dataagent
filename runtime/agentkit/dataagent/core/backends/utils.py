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
"""后端共用的纯函数。

分五组：

1. 路径：规范化、校验、相对化、子树重叠判断
2. glob：把一个 include 模式编译成匹配器（全后端共用同一套语义）
3. 文件数据：`FileData` 的创建 / 更新 / 取文本，读窗口的切片与分页
4. 文本编辑：精确字符串替换
5. 呈现：行号渲染、grep 结果格式化、结果截断

这些函数没有状态、不碰 IO，所以能脱离 agent 框架单独测。
"""

import os  # noqa: I001  # I001 记在导入块首行；logging 单独成段是模块级副作用的来源
import re
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any, Final, Literal, overload

import logging  # 与上面分开写，logger 是模块级副作用的来源
import wcmatch.glob as wcglob
from collections.abc import Callable, Sequence

from .protocol import FileData, FileInfo as FileInfo, GrepMatch, GrepResult, ReadResult

logger = logging.getLogger(__name__)

# ================================================================= 常量

EMPTY_CONTENT_WARNING = "System reminder: File exists but has empty contents"
EMPTY_OLD_STRING_ERROR = "Error: old_string cannot be empty. Provide the exact text to replace."

MAX_LINE_LENGTH = 5000
"""单行超过这个长度就在行号渲染时切成 `N.1` / `N.2` 续行。"""

TOOL_RESULT_TOKEN_LIMIT = 20000
"""工具结果的 token 预算，与驱逐阈值同一个数。"""

TRUNCATION_GUIDANCE = "... [results truncated, try being more specific with your parameters]"

MAX_VIDEO_INPUT_BYTES: Final = 1024 * 1024 * 1024
"""视频抽帧能接受的最大原始字节数（1 GiB）。"""

FileType = Literal["text", "image", "audio", "video", "file"]
"""按扩展名对文件做的粗分类。认不出来的一律当 text。"""

# 分类表按类型分组写，再合成一张扩展名 → 类型的表。
# 取值范围跟着 Google 多模态 API 支持的格式走。
_IMAGE_SUFFIXES = frozenset({".png", ".jpeg", ".jpg", ".webp", ".gif", ".heic", ".heif"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".mpeg", ".mov", ".avi", ".flv", ".mpg", ".webm", ".wmv", ".3gpp"})
_AUDIO_SUFFIXES = frozenset({".wav", ".mp3", ".aiff", ".aac", ".ogg", ".flac"})
_DOC_SUFFIXES = frozenset({".pdf", ".ppt", ".pptx"})

_SUFFIX_KIND: dict[str, FileType] = {
    suffix: kind
    for kind, suffixes in (
        ("image", _IMAGE_SUFFIXES),
        ("video", _VIDEO_SUFFIXES),
        ("audio", _AUDIO_SUFFIXES),
        ("file", _DOC_SUFFIXES),
    )
    for suffix in suffixes
}

_BINARY_ONLY_VIDEO_SUFFIXES: frozenset[str] = frozenset({".mkv"})
"""不在上面分类表里、但后端必须按二进制读的视频容器。

故意不放进 `_SUFFIX_KIND`：没装视频可选依赖时，`read_file` 应该把它当普通文件块
返回而不是原生视频块。但后端绝不能把它当文本解码——否则一个 UTF-8 解码就把字节毁了。
"""


class InvalidGlobPatternError(ValueError):
    """glob 编译被共用匹配器拒绝。

    基类仍是 `ValueError`，旧的 `except ValueError` 继续接得住。想分开原因时再单独
    接这个类型：同一次调用里的普通 `ValueError` 也可能是路径规范化失败。两件事搅在
    一起，模型会去改一个其实合法的模式。
    """


# ================================================================= 一、路径


def to_posix_path(path: str) -> str:
    r"""把路径里的 `\` 全部换成 `/`。

    后面会拿结果去建 `PurePosixPath`。那个类不把反斜杠当分隔符，`.name` 会吞进整段。
    能不能换不能看 `os.sep`：非 Windows 进程一样会收到 `C:\...`，按本机分隔符判断会漏掉。

    POSIX 上目录名本身若含反斜杠，也会被改掉。这种名字极少，换分隔符时接受这个副作用。
    """
    return path.replace("\\", "/")


def validate_path(path: str, *, allowed_prefixes: Sequence[str] | None = None) -> str:
    r"""把虚拟路径收成以 `/` 开头的正斜杠形式，并拒绝穿越。

    `allowed_prefixes` 若给出，收完之后必须命中其中某一条。盘符路径（`C:/...`）
    不收：虚拟文件系统里这种写法有歧义。`~` 以及作为路径组件的 `..` 直接拒绝。

    Args:
        path: 原始路径。
        allowed_prefixes: 可选的允许前缀。

    Returns:
        收干净之后的绝对路径。

    Raises:
        ValueError: 穿越、家目录展开、盘符路径，或前缀不在允许列表里。

    Examples:
        >>> validate_path("foo/bar")
        '/foo/bar'
        >>> validate_path("/./foo//bar")
        '/foo/bar'
    """
    # "foo..bar.txt" 里有连续的两个点，但不是一个路径组件。用子串判断会误伤这种文件名。
    if ".." in PurePosixPath(to_posix_path(path)).parts or path.startswith("~"):
        msg = f"Path traversal not allowed: {path}"
        raise ValueError(msg)

    if re.match(r"^[a-zA-Z]:", path):
        msg = (
            f"Windows absolute paths are not supported: {path}. "
            "Please use virtual paths starting with / (e.g., /workspace/file.txt)"
        )
        raise ValueError(msg)

    canonical = to_posix_path(os.path.normpath(path))
    if not canonical.startswith("/"):
        canonical = f"/{canonical}"

    # 兜一道：确认 normpath 自己没造出穿越
    if ".." in canonical.split("/"):
        msg = f"Path traversal detected after normalization: {path} -> {canonical}"
        raise ValueError(msg)

    if allowed_prefixes is not None and not any(canonical.startswith(p) for p in allowed_prefixes):
        msg = f"Path must start with one of {allowed_prefixes}: {path}"
        raise ValueError(msg)

    return canonical


def _normalize_path(path: str | None) -> str:
    """把搜索根路径归一成「以 / 开头、除根之外不带尾斜杠」的形式。

    `None` 当成根。与 `validate_path` 的区别：这个只做形状归一，不做安全校验。

    Raises:
        ValueError: 路径是空串或只有空白。
    """
    raw = path or "/"
    if not raw.strip():
        msg = "Path cannot be empty"
        raise ValueError(msg)

    shaped = raw if raw.startswith("/") else f"/{raw}"
    return shaped if shaped == "/" else shaped.rstrip("/")


def _relative_to_root(file_path: str, normalized_path: str) -> str:
    """把绝对路径转成相对搜索根的形式，给 glob 匹配用。

    搜索根本身就是那个文件时（精确文件搜索），返回文件名。
    """
    if normalized_path == "/":
        return file_path[1:]
    if file_path == normalized_path:
        return file_path.rsplit("/", maxsplit=1)[-1]
    return file_path[len(normalized_path) + 1 :]


# 通配段用的字符，对应 wcmatch 的 BRACE 与 GLOBSTAR。`_glob_anchor` 靠它截前导目录。
_WILDCARD_CHARS = frozenset("*?[{")


def _glob_anchor(pattern: str) -> str:
    """截出 `pattern` 开头那段没有通配符的目录。

    根上就有通配符时（`/**/secrets`）只能返回 `/`，重叠检查因此会罩住任意子树。
    静态看不准规则落点，所以宁可多拦。调用方若要收窄，应把规则前几段写成确定目录。

    例子：`/secrets/**` 得到 `/secrets`，`/a/*/b` 得到 `/a`。
    """
    fixed: list[str] = []
    for part in PurePosixPath(to_posix_path(pattern)).parts:
        if _WILDCARD_CHARS & set(part):
            break
        fixed.append(part)
    return str(PurePosixPath(*fixed)) if fixed else "/"


def _paths_overlap(call_path: str, rule_anchor: str) -> bool:
    """判断两棵子树有没有交集。

    相等，或者按 `PurePosixPath` 的组件看其中一棵包含另一棵，算相交。
    `/secret` 碰不到 `/secrets`。`/` 和谁都相交。
    """
    left, right = PurePosixPath(call_path), PurePosixPath(rule_anchor)
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


# ================================================================= 二、glob


@lru_cache(maxsize=256)
def compile_grep_include_glob(pattern: str) -> Callable[[str], bool]:
    """编译 grep 与 glob 共用的 include 模式。

    点目录和取反最容易写反：

    - 没开 DOTMATCH。模式段自己不以 `.` 开头时，点开头的名字不命中，`**` 也不进入点目录。
      所以 `*.yml` 能命中 `.github/workflows/ci.yml`，`**/*.yml` 不能。
    - 前导 `!` 不是取反，按字面量留在模式里。

    范围：

    - 没有 `/`：匹配任意深度的 basename。`*.py` 命中 `src/app/main.py`。
    - 有 `/`：相对搜索根，认 `**`。`src/**/*.py` 命中 `src/app/main.py`。
    - 以 `/` 开头：锚在搜索根，范围变小。`/*.py` 命中 `top.py`，不命中 `src/app/main.py`。

    各后端都走这一份。装没装 ripgrep，不影响 `glob=` 的这些结果。

    Args:
        pattern: include 模式。

    Returns:
        判定函数。参数是相对搜索根的 POSIX 路径。

    Raises:
        InvalidGlobPatternError: 出现 `..` 组件，或 wcmatch 拒绝编译（大括号展开
            超过它的上限时会这样）。`*.{py`、`[a-` 这类残缺写法通常不抛，编译成功但匹配为空。
    """
    # 穿越若散落到各后端，有的会抛，有的会返回空。这里统一拦。
    if ".." in to_posix_path(pattern).split("/"):
        msg = f"Path traversal not allowed in glob pattern {pattern!r}"
        raise InvalidGlobPatternError(msg)

    # `/*.py` 算不算锚在根上，必须在去掉前导斜杠之前决定，否则会变成任意深度的 basename。
    root_anchored = "/" in pattern
    try:
        compiled = wcglob.compile(pattern.lstrip("/"), flags=wcglob.BRACE | wcglob.GLOBSTAR)
    except Exception as exc:
        # wcmatch 的失败类型是私有的（例如 PatternLimitException）。收成公开异常后，
        # 后端不必再 import 那个私有模块。日志要先打：非 str，或库升级带来的真错误，
        # 也会被这条宽捕获吃掉，调用方看到的「模式无效」可能并不是模式写错。
        logger.warning("wcmatch refused glob pattern %r (%s): %s", pattern, type(exc).__name__, exc)
        msg = f"Invalid glob pattern {pattern!r}: {exc}"
        raise InvalidGlobPatternError(msg) from exc

    if root_anchored:
        return lambda rel_path: bool(compiled.match(rel_path))
    return lambda rel_path: bool(compiled.match(PurePosixPath(rel_path).name))


def _filter_files_by_path(files: dict[str, Any], normalized_path: str) -> dict[str, Any]:
    """按搜索根筛出候选文件。根路径本身就是某个文件时，只返回那一个。

    入参的 `normalized_path` 应当来自 `_normalize_path`（除根之外不带尾斜杠）。
    """
    if normalized_path in files:
        return {normalized_path: files[normalized_path]}

    prefix = "/" if normalized_path == "/" else f"{normalized_path}/"
    return {path: data for path, data in files.items() if path.startswith(prefix)}


def _glob_search_files(files: dict[str, Any], pattern: str, path: str | None = None) -> str:
    """在内存 files 映射里按 glob 找文件，返回换行分隔的路径列表。

    语义见 `compile_grep_include_glob`。结果按 `modified_at` 倒序（没有时间戳的排最后）。
    没命中返回 `"No files found"`。

    Raises:
        InvalidGlobPatternError: 匹配器拒绝 `pattern`。注意 `path` 不合法**不会**抛，
            而是返回 `"No files found"`。
    """
    try:
        root = _normalize_path(path)
    except ValueError:
        return "No files found"

    matcher = compile_grep_include_glob(pattern)
    hits = [
        (file_path, data.get("modified_at", ""))
        for file_path, data in _filter_files_by_path(files, root).items()
        if matcher(_relative_to_root(file_path, root))
    ]
    if not hits:
        return "No files found"

    hits.sort(key=lambda pair: pair[1], reverse=True)
    return "\n".join(file_path for file_path, _ in hits)


# ================================================================= 三、文件数据


def _normalize_content(file_data: FileData) -> str:
    """取出文件内容的纯文本形式，兼容历史上 content 是 `list[str]` 的数据。

    Raises:
        TypeError: content 既不是字符串也不是字符串列表。
    """
    raw: object = file_data["content"]
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return "\n".join(raw)
    msg = f"File content must be a string or a legacy list of strings, got {type(raw).__name__}."
    raise TypeError(msg)


def file_data_to_string(file_data: FileData) -> str:
    """取文件内容的纯文本形式（`_normalize_content` 的公开名字）。"""
    return _normalize_content(file_data)


def _utc_now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


def create_file_data(content: str, created_at: str | None = None, encoding: str = "utf-8") -> FileData:
    """新建一个 `FileData`，两个时间戳都盖上。

    Args:
        content: 文本内容，或 base64 串（配 `encoding="base64"`）。
        created_at: 给了就用它当创建时间，否则用当前时间。
        encoding: `"utf-8"` 或 `"base64"`。
    """
    now = _utc_now_iso()
    fresh = FileData(content=content, encoding=encoding)
    fresh["created_at"] = created_at or now
    fresh["modified_at"] = now
    return fresh


def update_file_data(file_data: FileData, content: str) -> FileData:
    """换内容并刷新 `modified_at`，`created_at` 与 `encoding` 沿用原值。"""
    updated = FileData(content=content, encoding=file_data.get("encoding", "utf-8"))
    if "created_at" in file_data:
        updated["created_at"] = file_data["created_at"]
    updated["modified_at"] = _utc_now_iso()
    return updated


def _copy_file_data_with_content(file_data: FileData, content: str) -> FileData:
    """换内容但**不动时间戳**。

    与 `update_file_data` 的区别就在这里：切一个读窗口并没有改文件，不该刷新
    `modified_at`。
    """
    copied = FileData(content=content, encoding=file_data.get("encoding", "utf-8"))
    for stamp in ("created_at", "modified_at"):
        if stamp in file_data:
            copied[stamp] = file_data[stamp]  # type: ignore[literal-required]
    return copied


def normalize_read_bounds(offset: int, limit: int) -> tuple[int, int]:
    """把读窗口的 offset 和 limit 收成两个非负整数。

    沙箱后端会把这两个数嵌进要执行的脚本。它们来自模型填的工具参数，所以这里用
    `int()` 强制转换，不能因为注解已经写了 `int` 就删掉。

    负的 limit 收成 0 之后仍是空窗口，没有合法的起止行号。调用方看到 0 要当成
    「没请求任何行」，给结果标 `no_lines_requested`（`slice_read_response` 以及
    沙箱里几处等价短路都这么做）。只收 limit、不处理这个空窗口，不够。

    负的 offset 收成 0，避免报出从第 1 行之前开始的区间。那样的 `ReadResult` 会被拒。
    """
    floored = max(int(offset), 0), max(int(limit), 0)
    if floored != (offset, limit):
        logger.debug("Clamped degenerate read window: offset %r -> %d, limit %r -> %d", offset, floored[0], limit, floored[1])
    return floored


def slice_read_response(file_data: FileData, offset: int, limit: int) -> ReadResult:
    """切出一段原文，不添加行号。

    `offset` 与 `limit` 都先交给 `normalize_read_bounds`。负 offset 从第一行起，
    负 limit 变成 0。

    空内容、只有空白、以及 limit 收成 0 时，不带分页字段。limit 为 0 另标
    `no_lines_requested`，好和「文件本身是空的」分开。offset 超过末行时只填 `error`。
    """
    text = file_data_to_string(file_data)
    offset, limit = normalize_read_bounds(offset, limit)

    # 纯空白文件必须先返回原文。若先看 limit==0，limit 为 0 时会得到 ""，
    # 中间件就无法把它换成空文件提示。
    if not text or not text.strip():
        return ReadResult(file_data=_copy_file_data_with_content(file_data, text))

    if limit == 0:
        return ReadResult(file_data=_copy_file_data_with_content(file_data, ""), no_lines_requested=True)

    # keepends 切行再以空串拼回，末行有没有换行会留下来，edit 才能判断 EOF。
    # CR 和 CRLF 也在这一步切开，不必先把整个文件重写成 LF 再编号。
    lines = text.splitlines(keepends=True)
    total = len(lines)
    if offset >= total:
        return ReadResult(error=f"Line offset {offset} exceeds file length ({total} lines)")

    stop = min(offset + limit, total)
    # edit 的匹配、grep、行号格式化只认 LF。CRLF 与 CR 可以留在 state/store 的存储里，
    # 这里只改已经切出来的那一段。
    window = "".join(lines[offset:stop]).replace("\r\n", "\n").replace("\r", "\n")
    return ReadResult(
        file_data=_copy_file_data_with_content(file_data, window),
        total_lines=total,
        start_line=offset + 1,
        end_line=stop,
        next_offset=stop if stop < total else None,
    )


def _get_file_type(path: str) -> FileType:
    """按扩展名分类。认不出来的返回 `"text"`。"""
    return _SUFFIX_KIND.get(PurePosixPath(path).suffix.lower(), "text")


def _get_backend_read_file_type(path: str) -> FileType:
    """读文件前的类型。`.mkv` 这类容器不在扩展名表里。

    表里没有的后缀，`_get_file_type` 会给 `"text"`。后端用「不是 text」决定走二进制，
    于是这些容器会被按文本解码，或者把 base64 按行切开。返回 `"video"` 是为了让
    每条后端都进二进制读，而不是让缺省分类漏过去。
    """
    if PurePosixPath(path).suffix.lower() in _BINARY_ONLY_VIDEO_SUFFIXES:
        return "video"
    return _get_file_type(path)


# ================================================================= 四、文本编辑


ReplaceOutcome = tuple[str, int] | str


def perform_string_replacement(content: str, old_string: str, new_string: str, replace_all: bool = False) -> ReplaceOutcome:
    """精确字符串替换，带出现次数校验。

    Returns:
        成功返回 `(新内容, 替换次数)`；失败返回错误文案字符串。
    """
    if not old_string:
        return EMPTY_OLD_STRING_ERROR

    hits = content.count(old_string)

    if hits == 0:
        # 认一种很常见的 EOF 不匹配：old_string 末尾带了换行，而文件在同一位置没有。
        # 模型看到一行「长得挺完整」就会自己补个终止符。精确匹配的消费方必须把原因
        # 说准，而不是悄悄放宽契约——对着去掉换行的 key 做静默恢复，有可能改坏中间
        # 某段刚好前缀相同的文本。
        if old_string.endswith("\n") and len(old_string) > 1 and content.endswith(old_string.removesuffix("\n")):
            trimmed_hits = content.count(old_string.removesuffix("\n"))
            if trimmed_hits == 1:
                return (
                    "Error: old_string ends with a newline, but the file does not end with a newline. "
                    "Retry with the trailing newline removed from old_string (and from new_string if it also "
                    "ends with a newline)."
                )
            # 去掉换行之后仍然有歧义：模型得同时做两件事（去掉换行 + 补上下文）
            return (
                f"Error: old_string ends with a newline, but the file does not end with a newline. "
                f"With the trailing newline removed, old_string would appear {trimmed_hits} times in the file. "
                f"Retry with the trailing newline removed and add surrounding context so the match is unique."
            )
        return f"Error: String not found in file: '{old_string}'"

    if hits > 1 and not replace_all:
        return (
            f"Error: String '{old_string}' appears {hits} times in file. "
            f"Use replace_all=True to replace all instances, or provide a more specific string with surrounding context."
        )

    return content.replace(old_string, new_string), hits


# ================================================================= 五、呈现


def sanitize_tool_call_id(tool_call_id: str) -> str:
    r"""把 tool_call_id 里的 `.` `/` `\` 换成下划线。

    这个 id 会被拼进落盘路径，所以先掐掉路径分隔符与相对路径的可能。
    """
    return tool_call_id.replace(".", "_").replace("/", "_").replace("\\", "_")


def _split_source_lines(content: str | list[str]) -> list[str]:
    """把内容拆成行。字符串形式下，末尾那个空串（来自尾换行）要丢掉。"""
    if not isinstance(content, str):
        return content
    lines = content.split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def format_content_with_line_numbers(content: str | list[str], start_line: int = 1) -> str:
    """给内容加行号。

    超过 `MAX_LINE_LENGTH` 的行会被切成多块，续块的行号标成 `N.1`、`N.2`。
    行号与正文之间固定隔**两个空格**，这样源码里的 Tab 不会被误当成行号分隔符。
    """
    rows: list[tuple[str, str]] = []
    for index, line in enumerate(_split_source_lines(content)):
        number = index + start_line
        # 每 MAX_LINE_LENGTH 一块；短行只会得到一块。`or [line]` 保证空行也占一行
        # ——否则空区间会让它整行消失、连行号都没有。
        chunks = [line[at : at + MAX_LINE_LENGTH] for at in range(0, len(line), MAX_LINE_LENGTH)] or [line]
        rows.extend((str(number) if idx == 0 else f"{number}.{idx}", chunk) for idx, chunk in enumerate(chunks))

    # 两个空格的分隔符是**有下游依赖的契约**：TUI 会按它重排行号列，某些模型画像的
    # 中间件会按它数源码行来决定要不要追加「还有后续」的提示。缩短或改变它会静默
    # 破坏那些消费方。
    width = max((len(marker) for marker, _ in rows), default=0)
    return "\n".join(f"{marker:>{width}}  {text}" for marker, text in rows)


def _format_source_block(content: str | list[str]) -> str:
    """把内容拼成 `read_file` 结果里**逐字原样**的正文。

    源码行不做任何加工。唯一的结构元素是中间件放在正文上方的状态头，所以这里
    不需要转义什么。
    """
    return "\n".join(_split_source_lines(content))


def check_empty_content(content: str) -> str | None:
    """内容为空或只有空白时返回提示文案，否则返回 None。"""
    return EMPTY_CONTENT_WARNING if not content.strip() else None


@overload
def truncate_if_too_long(result: list[str]) -> list[str]: ...


@overload
def truncate_if_too_long(result: str) -> str: ...


def truncate_if_too_long(result: list[str] | str) -> list[str] | str:
    """结果超出 token 预算就截断（粗算 4 字符 / token）。"""
    budget = TOOL_RESULT_TOKEN_LIMIT * 4

    if isinstance(result, str):
        if len(result) <= budget:
            return result
        return result[: budget - len(TRUNCATION_GUIDANCE) - 1] + "\n" + TRUNCATION_GUIDANCE

    # 列表最终会被调用方用 str() 渲染，所以每项的成本是它的 repr 加上 ", "
    allowance = budget - len(repr(TRUNCATION_GUIDANCE)) - 2
    spent = 0
    for kept, item in enumerate(result):
        spent += len(repr(item)) + 2
        if spent > allowance:
            return [*result[:kept], TRUNCATION_GUIDANCE]
    return result


def build_grep_results_dict(matches: list[GrepMatch]) -> dict[str, list[tuple[int, str]]]:
    """把结构化命中按文件分组，转成格式化函数用的旧形态。"""
    grouped: dict[str, list[tuple[int, str]]] = {}
    for hit in matches:
        grouped.setdefault(hit["path"], []).append((hit["line"], hit["text"]))
    return grouped


GrepOutputMode = Literal["files_with_matches", "content", "count"]


def _format_grep_results(results: dict[str, list[tuple[int, str]]], output_mode: GrepOutputMode) -> str:
    """按输出模式渲染分组后的 grep 结果。"""
    paths = sorted(results)
    if output_mode == "files_with_matches":
        return "\n".join(paths)
    if output_mode == "count":
        return "\n".join(f"{path}: {len(results[path])}" for path in paths)

    lines: list[str] = []
    for path in paths:
        lines.append(f"{path}:")
        lines.extend(f"  {number}: {text}" for number, text in results[path])
    return "\n".join(lines)


def _group_adjacent_lines(displayed_lines: dict[int, str]) -> list[list[tuple[int, str]]]:
    """把 `{行号: 正文}` 切成若干段连续行号的组。"""
    groups: list[list[tuple[int, str]]] = []
    for entry in sorted(displayed_lines.items()):
        if groups and entry[0] <= groups[-1][-1][0] + 1:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    return groups


def _format_grep_with_context(matches: list[GrepMatch]) -> str:
    """渲染带上下文行的 content 模式输出。

    命中行用 `:` 标，上下文行用 `-` 标；同一文件里不相邻的行组之间插一行 `--`，
    与 `grep -C` 的观感一致。
    """
    by_path: dict[str, list[GrepMatch]] = {}
    for hit in matches:
        by_path.setdefault(hit["path"], []).append(hit)

    lines: list[str] = []
    for path in sorted(by_path):
        file_hits = by_path[path]
        hit_numbers = {hit["line"] for hit in file_hits}

        shown: dict[int, str] = {}
        for hit in file_hits:
            for ctx in hit.get("context_before", []):
                shown[ctx["line"]] = ctx["text"]
            shown[hit["line"]] = hit["text"]
            for ctx in hit.get("context_after", []):
                shown[ctx["line"]] = ctx["text"]

        lines.append(f"{path}:")
        for position, group in enumerate(_group_adjacent_lines(shown)):
            if position:
                lines.append("  --")
            lines.extend(f"  {number}{':' if number in hit_numbers else '-'} {text}" for number, text in group)
    return "\n".join(lines)


def format_grep_matches(matches: list[GrepMatch], output_mode: GrepOutputMode) -> str:
    """渲染结构化 grep 命中。"""
    if not matches:
        return "No matches found"

    # 只要有任何一条带上下文键，整个结果就按「上下文模式」渲染；生产方要么每条都带、
    # 要么都不带。但这个函数是公开的，可能被手工构造的混合输入调用，所以
    # `_format_grep_with_context` 自己也容忍混合。
    has_context = any("context_before" in hit or "context_after" in hit for hit in matches)
    if output_mode != "content" or not has_context:
        return _format_grep_results(build_grep_results_dict(matches), output_mode)
    return _format_grep_with_context(matches)


def grep_matches_from_files(
    files: dict[str, Any], pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None
) -> GrepResult:
    """在内存里的 files 上做字面量子串搜索。

    不抛异常。`glob` 被拒绝时，错误文案放进 `GrepResult(error=...)`。工具层要的是
    这条文案，不是栈。

    `max_count` 是条数上限。扫到更多才停，并设 `truncated=True`。条数刚好等于上限、
    没有被丢掉的，`truncated` 仍是 `False`。
    """
    try:
        root = _normalize_path(path)
    except ValueError:
        return GrepResult(matches=[])

    candidates = _filter_files_by_path(files, root)

    if glob:
        try:
            keep = compile_grep_include_glob(glob)
        except InvalidGlobPatternError as bad_glob:
            return GrepResult(error=str(bad_glob))
        candidates = {p: d for p, d in candidates.items() if keep(_relative_to_root(p, root))}

    found: list[GrepMatch] = []
    for file_path, data in candidates.items():
        for number, line in enumerate(_normalize_content(data).split("\n"), 1):
            if pattern not in line:
                continue
            # 计数在写入之前做：已经攒满再遇到下一条，才说明被截断。
            # 刚好满、后面没有下一条时，不进这个分支，truncated 保持假。
            if max_count is not None and len(found) >= max_count:
                return GrepResult(matches=found, truncated=True)
            found.append({"path": file_path, "line": int(number), "text": line})
    return GrepResult(matches=found)


# 单独的 `.` `(` `)` `[` `]` `?` `^` `$` 在字面量里太常见（方法调用、下标），不算信号。
# 算信号的是竖线、`.*`、`.+`，以及带反斜杠的元字符或字符类。
_REGEX_SIGNALS = re.compile(
    r"\|"
    r"|\.\*"
    r"|\.\+"
    r"|\\[.wWdDsSbB(){}\[\]|+*?^$]"
)


#: 这段会进入模型上下文，属于行为契约：措辞与上游保持一致，不得改写（见 06-cleanroom规则.md 第三节）
_REGEX_HINT_TEXT = (
    "Note: grep matches literal text, not regex, so characters like "
    "`|`, `.*`, and `\\.` are searched verbatim. Search for the literal "
    "text you need instead; for `|` alternation, run a separate search "
    "per alternative."
)


def _looks_like_regex(pattern: str) -> bool:
    """启发式判断：这个本该是字面量的 grep 模式里是否有正则语法。"""
    return bool(_REGEX_SIGNALS.search(pattern))


def regex_literal_hint(pattern: str) -> str | None:
    """像正则的模式返回提示，否则返回 None。

    这个函数只检查模式本身。调用方应在一次搜索没有命中之后再来问：grep 按字面量
    找，元字符不会被解释，所以会静默落空。
    """
    if not _looks_like_regex(pattern):
        return None
    return _REGEX_HINT_TEXT
