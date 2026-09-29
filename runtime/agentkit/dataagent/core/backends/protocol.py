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
"""后端协议：接口、结果类型、能力探测。

这里没有任何一个具体后端。路径、glob、读窗口、替换与渲染都在 ``utils``，
本模块是 import 叶子——谁都不许从这里再引回去，否则和 ``utils`` 立刻成环。

两条贯穿全文件的约定：

- 失败写进结果的 ``error`` 字符串，不把异常扔给工具层。
- ``delete`` 可选。没覆写就留着基类那个 ``NotImplementedError``，调用方用
  ``_supports_delete`` 探测，或者自己接住。
"""

import abc
import asyncio
import dataclasses
import functools
import inspect
import logging
import typing

import typing_extensions

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 超时预算

DEFAULT_GREP_TIMEOUT = 15
"""单次同步 grep 阶段的超时（秒）。异步外层预算按它来推。"""

ASYNC_GREP_TIMEOUT = 2 * DEFAULT_GREP_TIMEOUT + 5
"""异步 grep 的等待上限。

要盖住「ripgrep 先超时，再退回 Python 再超时」这条最坏路径，所以是
两倍同步预算再加一点余量，而不是一个写死的秒数。它只约束调用方等多久，
停不掉已经丢进线程的搜索。
"""

ASYNC_GLOB_TIMEOUT = 30
"""沙箱 glob 一次往返的超时（秒）。远端脚本自己还有预算，这里兜的是卡死的传输。"""

# ---------------------------------------------------------------- 错误码与截断原因

FileOperationError = typing.Literal["file_not_found", "permission_denied", "is_directory", "invalid_path"]
"""上传 / 下载批量结果里，模型有机会自己纠正的那四种标准错误。"""

FILE_NOT_FOUND = "file_not_found"
PERMISSION_DENIED = "permission_denied"
IS_DIRECTORY = "is_directory"
INVALID_PATH = "invalid_path"

GlobTruncationReason = typing.Literal["budget", "unreadable", "transport"]
"""``glob`` 结果不完整时的原因。工具层按它给三种不同建议。

- ``budget``：撞上时间预算或数量上限，缩小范围还能拿到剩下的。
- ``unreadable``：某棵子树读不了（比如权限），缩小范围没有用。
- ``transport``：沙箱传输层把输出截断了。
"""

# 注解里反复出现的联合类型。运行时求值之后跟直接写 ``str | None`` 是同一个对象，
# 只是源码上不再把同一段联合类型抄十遍。
_OptionalStr: typing.TypeAlias = str | None
_OptionalInt: typing.TypeAlias = int | None
_OptionalBytes: typing.TypeAlias = bytes | None
_BatchError: typing.TypeAlias = FileOperationError | str | None
_OptionalReason: typing.TypeAlias = GlobTruncationReason | None
_UploadBatch: typing.TypeAlias = list[tuple[str, bytes]]
_PathBatch: typing.TypeAlias = list[str]


def _zero_read_collides(
    error: str | None,
    start_line: int | None,
    next_offset: int | None,
    total_lines: int | None,
) -> bool:
    """零行读只许带着 ``file_data``。错误或任何分页字段都算冲突。

    ``end_line`` 不在这张清单里：它和 ``start_line`` 不成对时，更早的一条规则会先拦住。
    """
    if error is not None:
        return True
    if start_line is not None:
        return True
    if next_offset is not None:
        return True
    return total_lines is not None


def _not_implemented() -> typing.NoReturn:
    """同步方法的默认出路：子类没覆写就明确失败，不要假装成功。"""
    raise NotImplementedError


async def _run_blocking(bound, /, *args, **kwargs):
    """异步默认实现的共同出口：把同步方法丢进线程，异常原样冒出来。"""
    return await asyncio.to_thread(bound, *args, **kwargs)


# ---------------------------------------------------------------- 结构化字典


class ExecuteArtifact(typing_extensions.TypedDict):
    """挂在 ``ToolMessage.artifact`` 上，和给模型看的 ``content`` 并行。

    命令根本没跑起来（校验失败、后端不支持）时 artifact 是 ``None`` 而不是空 dict，
    那种 ``ToolMessage.status`` 是 ``"error"``。只要命令跑了，status 就是 ``"success"``，
    哪怕退出码非零——判失败看 ``exit_code``。拿不到退出码时省略这个键，不要填 ``None``。
    """

    exit_code: typing.NotRequired[int]


class ContextLine(typing_extensions.TypedDict):
    """命中行旁边的一行上下文。``line`` 从 1 起算。"""

    line: int
    text: str


class GrepMatch(typing_extensions.TypedDict):
    """一条 grep 命中。

    ``context_before`` / ``context_after`` 要么都在、要么都不在：只有调用方要了
    上下文才会出现，出现时每条命中都有。空列表表示那一侧没有可取的上下文——到了
    文件边界、相邻行自己也是命中（命中不重复当上下文）、或那次重读失败（失败另外
    记在 ``GrepResult.error``）。
    """

    path: str
    line: int
    text: str
    context_before: typing.NotRequired[list[ContextLine]]
    context_after: typing.NotRequired[list[ContextLine]]


class FileData(typing_extensions.TypedDict):
    """一个文件的内容与可选时间戳。

    ``content`` 是纯文本（``encoding="utf-8"``）或 base64 串（``encoding="base64"``）。
    两个时间戳是可选的 ISO 8601 字符串。
    """

    content: str
    encoding: str
    created_at: typing.NotRequired[str]
    modified_at: typing.NotRequired[str]


class FileInfo(typing_extensions.TypedDict):
    """目录列举和 glob 的一条结果。只有 ``path`` 必填，其余各后端尽力给。

    ``glob`` 返回的 ``path`` 必须是绝对路径。权限拒绝规则只对绝对路径匹配，
    返回相对路径会让每一条 deny 被静默绕过。
    """

    path: str
    is_dir: typing.NotRequired[bool]
    size: typing.NotRequired[int]
    modified_at: typing.NotRequired[str]


_OptionalFileData: typing.TypeAlias = FileData | None
_OptionalInfos: typing.TypeAlias = list[FileInfo] | None
_OptionalHits: typing.TypeAlias = list[GrepMatch] | None


# ---------------------------------------------------------------- 结果对象


@dataclasses.dataclass
class ExecuteResponse:
    """一次 ``execute`` 的结果。

    非零退出不是失败：命令跑起来了，``ToolMessage.status`` 仍是 success，
    失败与否看 ``exit_code``。``None`` 表示拿不到退出码，不表示成功；``0`` 才是成功。
    ``truncated`` 表示输出撞了尺寸上限，和「全文留在沙箱」不是一回事。
    """

    output: str
    exit_code: _OptionalInt = None
    truncated: bool = False


@dataclasses.dataclass
class LsResult:
    """``ls`` 的结果。失败时 ``entries`` 为 ``None``。"""

    error: _OptionalStr = None
    entries: _OptionalInfos = None


@dataclasses.dataclass
class EditResult:
    """``edit`` 的结果。``occurrences`` 是实际替换了几处。"""

    error: _OptionalStr = None
    path: _OptionalStr = None
    occurrences: _OptionalInt = None


@dataclasses.dataclass
class GrepResult:
    """``grep`` 的结果。

    ``truncated=True`` 不是失败：搜索提前停了，``matches`` 里的东西仍然有效、只是不全。
    只有硬失败才让 ``matches`` 为 ``None``。恰好命中 ``max_count`` 条且一条都没丢，
    算完整，``truncated`` 保持原值。
    """

    error: _OptionalStr = None
    matches: _OptionalHits = None
    truncated: bool = False


@dataclasses.dataclass
class ReadResult:
    """``read`` 的一个窗口，或者一个错误。

    四个窗口字段不是互相独立的。与其让一个错的 ``next_offset`` 流到中间件、
    悄悄跳过没展示的行，不如在构造时就拒绝。

    数值关系：``end_line`` 是 1 起的「最后一行」，``next_offset`` 是 0 起的「下一行」，
    所以两者相等。读到文件末尾时 ``next_offset`` 不设，这是合法的最后一页。
    """

    error: _OptionalStr = None
    file_data: _OptionalFileData = None
    total_lines: _OptionalInt = None
    start_line: _OptionalInt = None
    end_line: _OptionalInt = None
    next_offset: _OptionalInt = None
    no_lines_requested: bool = False

    def __post_init__(self) -> None:
        """按固定顺序检查共现与数值。顺序本身是行为：先命中的那条决定异常文案。"""
        start = self.start_line
        end = self.end_line
        both_ends = start is not None and end is not None
        if (start is None) != (end is None):
            raise ValueError("ReadResult 的 start_line 与 end_line 必须同时给出，或同时留空")

        if self.no_lines_requested and _zero_read_collides(self.error, start, self.next_offset, self.total_lines):
            raise ValueError(
                "ReadResult.no_lines_requested 表示窗口没有被检查过，"
                "不能同时带 error、start_line、next_offset 或 total_lines"
            )

        if self.next_offset is not None and not both_ends:
            raise ValueError("ReadResult 给出了 next_offset，但没有成对的 start_line/end_line")
        if self.total_lines is not None and not both_ends:
            raise ValueError("ReadResult 给出了 total_lines，但没有成对的 start_line/end_line")
        if not both_ends:
            return

        # both_ends 为真时两个端点都是 int。
        if start < 1 or end < start:
            raise ValueError(
                f"ReadResult 的窗口必须满足 1 <= start_line <= end_line，收到 start_line={start}, end_line={end}"
            )
        if self.total_lines is not None and self.total_lines < end:
            raise ValueError(f"ReadResult 的 total_lines={self.total_lines} 不能小于 end_line={end}")
        if self.next_offset is not None and self.next_offset != end:
            raise ValueError(f"ReadResult 的 next_offset={self.next_offset} 必须等于 end_line={end}")


@dataclasses.dataclass
class FileDownloadResponse:
    """批量下载里的一条。返回顺序与入参路径严格对齐，允许部分成功。

    ``error`` 优先用 ``FileOperationError`` 的四个标准码，归一不了再用后端自己的字符串。
    ``path`` 带在结果里，是为了批量结果对得上号、出错时说得清。
    """

    path: str
    content: _OptionalBytes = None
    error: _BatchError = None


@dataclasses.dataclass
class WriteResult:
    """``write`` 的结果。``path`` 是实际写入的绝对路径。"""

    error: _OptionalStr = None
    path: _OptionalStr = None


@dataclasses.dataclass
class GlobResult:
    """``glob`` 的结果。只含普通文件，不含目录。

    ``truncated=True`` 不是失败。``truncation_reason`` 只在截断且产出方分得清原因时才设，
    其余情况为 ``None``。匹配器拒绝的模式（大括号展开超限、含 ``..`` 段）走
    ``error`` 加 ``matches=None``，不抛异常。
    """

    error: _OptionalStr = None
    matches: _OptionalInfos = None
    truncated: bool = False
    truncation_reason: _OptionalReason = None


@dataclasses.dataclass
class DeleteResult:
    """``delete`` 的结果。删除是递归的：目标本身以及它下面的一切。"""

    error: _OptionalStr = None
    path: _OptionalStr = None


@dataclasses.dataclass
class FileUploadResponse:
    """批量上传里的一条。返回顺序与入参文件严格对齐，允许部分成功。"""

    path: str
    error: _BatchError = None


@dataclasses.dataclass(frozen=True, slots=True)
class ExecuteOffloadResult:
    """带「全文留在沙箱」标记的执行结果。

    ``offloaded=True`` 时 ``response.output`` 只是首尾预览，全文在沙箱的捕获路径上；
    ``False`` 时 ``output`` 就是完整输出。``response.truncated`` 是另一件事：输出撞了
    尺寸上限。这个标记故意不放进 ``ExecuteResponse``，因为普通 ``execute`` 永远不会设它。
    """

    offloaded: bool
    response: ExecuteResponse


# ---------------------------------------------------------------- 协议


class BackendProtocol(abc.ABC):  # noqa: B024  # 要 register()，但不能逼子类实现全部方法
    """文件后端的同步 / 异步接口。

    继承 ``abc.ABC`` 是为了让第三方还能 ``BackendProtocol.register(...)`` 做虚子类。
    方法一律**不**标 ``abstractmethod``：只实现子集的后端也要能直接实例化，
    没实现的方法保持这里的 ``NotImplementedError``。

    **下面每个方法的路径参数都是绝对路径，必须以 ``/`` 开头**；``glob`` 返回的
    ``FileInfo.path`` 同样如此。这不是排版口味：``utils.validate_path`` 按它判定，
    各后端的权限 deny 规则也只对绝对路径匹配——放相对路径进来，每一条 deny 都会被
    静默绕过，而且不报错。

    读到的是未加行号的原文。行号、gutter、超长行的续行（``5.1`` / ``5.2``）由文件工具
    中间件调用 ``backends/utils.py`` 的渲染函数补上，不在后端做。
    """

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """读一个窗口的原文。

        协议层 ``limit`` 默认 2000，工具层 ``read_file`` 的默认是 100，两个数不要并成一个。
        实现方要容忍退化窗口，不要抛：负 ``offset`` 从第一行读；``limit <= 0`` 返回空内容，
        并且所有分页字段都不设。切片前可以用 ``utils.normalize_read_bounds`` 压边界。

        只要返回的是可编号的文本，就必须设 ``start_line``。中间件只在它缺失时才退回用
        ``offset`` 推 gutter，而那个推导只对「后端真的切过的窗口」才正确。
        """
        _not_implemented()

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        """把 ``old_string`` 换成 ``new_string``。``old_string`` **精确匹配**，空白与缩进算数。

        ``replace_all`` 为假（默认）时 ``old_string`` **必须在文件里唯一**：出现多次
        这次编辑整条**失败**（走 ``error``），不是挑第一处改。为真时把全部出现处一起换。

        ``new_string`` 与 ``old_string`` 相同**不是**错误：后端不校验这一条，替换退化
        成空操作，按成功返回。「必须与 old_string 不同」是文件工具中间件写在工具
        schema 里给模型看的，不要在后端补一个守卫去实现它——那会多出上游没有的行为。
        """
        _not_implemented()

    def grep(
        self, pattern: str, path: _OptionalStr = None, glob: _OptionalStr = None, *, max_count: _OptionalInt = None
    ) -> GrepResult:
        """按字面量子串搜索，不是正则。

        ``path`` 不给就搜后端的默认根。``glob`` 过滤的是文件名 / 路径，不是内容。
        ``max_count`` 是跨全部文件的总上限，不是每文件上限；恰好命中这么多条且没有丢弃，
        ``truncated`` 为假。通配符：``*`` 文件名内任意字符、``**`` 递归目录、``?`` 单字符、
        ``[abc]`` 字符集。
        """
        _not_implemented()

    def upload_files(self, files: _UploadBatch) -> list[FileUploadResponse]:
        """批量上传。允许部分成功，``response[i]`` 对应 ``files[i]``。

        这条 API 既能被开发者直接调，也能包成自定义工具暴露给模型。
        """
        _not_implemented()

    def ls(self, path: str) -> LsResult:
        """列出 ``path`` 下的条目。"""
        _not_implemented()

    def delete(self, file_path: str) -> DeleteResult:
        """递归删除 ``file_path`` 以及它下面的一切。

        层级后端（如文件系统）删目录及其内容；键值后端删精确 key，外加所有以
        ``file_path + "/"`` 为前缀的 key。这是可选方法，不实现就留着这个默认实现。
        """
        _not_implemented()

    def glob(self, pattern: str, path: _OptionalStr = None) -> GlobResult:
        """按与 grep include-glob 对齐的语义找文件，不是 shell 那种经典的非递归 glob。

        不含 ``/`` 的模式匹配 ``path`` 之下任意深度的 basename：``*.py`` 能匹配
        ``src/app/main.py``。含 ``/`` 的模式相对搜索根匹配，支持 ``**``：
        ``src/**/*.py`` 匹配 ``src/app/main.py``。前导 ``/`` 把模式锚在搜索根上，
        是收窄不是放宽：``/*.py`` 匹配 ``top.py``，不匹配 ``src/app/main.py``。

        点开头的名字只在模式那一段自己也以 ``.`` 开头时才匹配。``**`` 不会下潜进点目录，
        所以裸模式比它的 ``**/`` 形式更宽：``*.yml`` 能匹配 ``.github/workflows/ci.yml``，
        ``**/*.yml`` 不能；``.env`` 匹配 ``.env``，``*`` 不匹配。

        通配符比上面 ``grep`` 那段列的多两个：``*`` 段内任意字符、``**`` 递归目录、
        ``?`` 单字符、``[abc]`` 字符集、``[!abc]`` 取反、``{a,b}`` 大括号展开（含嵌套组）。
        多出来的是后两个——两边其实是同一个 ``utils.compile_grep_include_glob``，
        差的只是 grep 那段没列它们。大括号展开有上限，超了和含 ``..`` 段一样属于
        「匹配器拒绝」：走 ``GlobResult(error=...)`` 且 ``matches`` 保持 ``None``，不抛异常。

        只返回普通文件。``FileInfo.path`` 永远是绝对路径。
        """
        _not_implemented()

    def write(self, file_path: str, content: str) -> WriteResult:
        """把 ``content`` 写到 ``file_path``：不存在就创建，已存在就**整体覆盖**。

        没有追加语义，也没有部分更新——只想改其中一段用 ``edit``。
        """
        _not_implemented()

    def download_files(self, paths: _PathBatch) -> list[FileDownloadResponse]:
        """批量下载。允许部分成功，``response[i]`` 对应 ``paths[i]``。"""
        _not_implemented()

    async def adelete(self, file_path: str) -> DeleteResult:
        """``delete`` 的异步默认实现：丢进线程，不另写一套语义。"""
        return await _run_blocking(self.delete, file_path)

    async def aglob(self, pattern: str, path: _OptionalStr = None) -> GlobResult:
        """``glob`` 的异步默认实现。"""
        return await _run_blocking(self.glob, pattern, path)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """``write`` 的异步默认实现。"""
        return await _run_blocking(self.write, file_path, content)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """``read`` 的异步默认实现。签名与同步版一致，含 ``limit=2000``。"""
        return await _run_blocking(self.read, file_path, offset, limit)

    async def aupload_files(self, files: _UploadBatch) -> list[FileUploadResponse]:
        """``upload_files`` 的异步默认实现。"""
        return await _run_blocking(self.upload_files, files)

    async def als(self, path: str) -> LsResult:
        """``ls`` 的异步默认实现。"""
        return await _run_blocking(self.ls, path)

    async def aedit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """``edit`` 的异步默认实现。"""
        return await _run_blocking(self.edit, file_path, old_string, new_string, replace_all)

    async def adownload_files(self, paths: _PathBatch) -> list[FileDownloadResponse]:
        """``download_files`` 的异步默认实现。"""
        return await _run_blocking(self.download_files, paths)

    async def agrep(
        self, pattern: str, path: _OptionalStr = None, glob: _OptionalStr = None, *, max_count: _OptionalInt = None
    ) -> GrepResult:
        """带超时安全网的异步搜索。

        超时只限制调用方等多久，``asyncio.to_thread`` 里的工作线程不会因此停掉。
        这是安全网，不是取消。上限双保险：同步 ``grep`` 收 ``max_count`` 就把上限
        下推，让搜索自己收敛；不收就跑完再裁。两条路的返回值都再过一遍
        ``_apply_grep_max_count``（已经在上限内时它什么都不改），调用方拿到的保证一样。

        只接 ``TimeoutError``。同步实现抛出的 ``NotImplementedError`` 等其它异常原样穿透。
        超时后 ``matches`` 保持 ``None``。下面这句 ``error`` 会进模型上下文，措辞不能改。
        """
        pushed: dict[str, int | None] = {}
        if _method_accepts_max_count(type(self), "grep"):
            pushed["max_count"] = max_count
        try:
            # 必须走 asyncio.wait_for 这个属性，并且 timeout 用关键字。
            # 测试按模块属性打桩，签名是 (coro, *, timeout)。
            found = await asyncio.wait_for(
                asyncio.to_thread(self.grep, pattern, path, glob, **pushed),
                timeout=ASYNC_GREP_TIMEOUT,
            )
        except TimeoutError:
            logger.warning(
                "agrep 等待超过 %s 秒（线程不会被取消）：pattern=%r path=%r glob=%r",
                ASYNC_GREP_TIMEOUT, pattern, path, glob,
            )
            return GrepResult(
                error=f"Error: grep timed out after {ASYNC_GREP_TIMEOUT}s. Try a more specific pattern or a narrower path.",
            )
        return _apply_grep_max_count(found, max_count)


class SandboxBackendProtocol(BackendProtocol):
    """在文件操作之外还能执行命令的后端。

    普通子类，不再叠一层 ABC，也不加抽象方法——什么都没实现的子类也要能实例化。
    ``execute`` 的非零退出不是协议层的失败，看 ``exit_code``。
    """

    @property
    def id(self) -> str:
        """这个沙箱实例的唯一标识。是属性，不是方法。"""
        _not_implemented()

    def execute(self, command: str, *, timeout: _OptionalInt = None) -> ExecuteResponse:
        """执行一条命令。``timeout`` 只能按关键字传。

        ``None`` 用后端自己的默认值。``0`` 在支持「无超时执行」的后端上可能表示关掉超时。
        调用方要跨后端一致的行为，就传非负整数。
        """
        _not_implemented()

    async def aexecute(self, command: str, *, timeout: _OptionalInt = None) -> ExecuteResponse:
        """``execute`` 的异步默认实现。

        老后端的 ``execute`` 没有 ``timeout`` 参数时，这里把超时静默丢掉，不报错。
        面向用户的报错在中间件那一层；这里的守卫只保护绕过中间件的直接调用方。
        ``timeout is None`` 时不去内省签名，直接按后端自己的默认值跑。
        """
        if timeout is not None and execute_accepts_timeout(type(self)):
            return await _run_blocking(self.execute, command, timeout=timeout)
        return await _run_blocking(self.execute, command)


# ---------------------------------------------------------------- 能力探测
# 这三个函数的注解要即时求值，所以必须放在协议类之后。
# 不要靠 `from __future__ import annotations` 把它们提前——那会把 TypedDict 的
# NotRequired 全部变成必填。


def _apply_grep_max_count(result: GrepResult, max_count: int | None) -> GrepResult:
    """搜索跑完之后再卡总条数。

    没给上限、硬失败（``matches is None``）、或者一条都没被丢掉时，返回传入的那个对象，
    不复制、也不把 ``truncated`` 改成真。只有真的切掉了尾部才返回新结果。

    ``max_count is None`` 必须写在这条 ``or`` 链的最前面：老后端的 ``grep`` 可能返回
    list 或错误字符串，默认路径要原样透传，在这之前不能读 ``result`` 的任何属性。
    """
    if max_count is None or result.matches is None or len(result.matches) <= max_count:
        return result
    kept = result.matches[:max_count]
    return GrepResult(error=result.error, matches=kept, truncated=True)


@functools.lru_cache(maxsize=256)
def _method_accepts_max_count(cls: type[BackendProtocol], method_name: typing.Literal["grep", "agrep"]) -> bool:
    """这个后端类的 ``grep`` / ``agrep`` 收不收 ``max_count`` 关键字。

    收就把上限下推，让搜索自己收敛；不收就跑完再裁。用来兼容没跟上签名变更的老第三方后端。
    ``**kwargs`` 算支持（上限会被转进去）。内省失败按不支持处理，同一个类只看一次。
    """
    try:
        signature = inspect.signature(getattr(cls, method_name))
    except (AttributeError, ValueError, TypeError):
        logger.warning(
            "无法内省 %s.%s 的签名，按不支持 max_count 处理",
            cls,
            method_name,
            exc_info=True,
        )
        return False
    if "max_count" in signature.parameters:
        return True
    return any(param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())


def _supports_delete(backend: BackendProtocol) -> bool:
    """``delete`` 有没有被覆写。比的是函数对象是不是同一个，不去调用、也不看 hasattr。"""
    implemented = type(backend).delete
    baseline = BackendProtocol.delete
    return implemented is not baseline


@functools.lru_cache(maxsize=128)
def execute_accepts_timeout(cls: type[SandboxBackendProtocol]) -> bool:
    """这个沙箱类的 ``execute`` 收不收名为 ``timeout`` 的参数。

    老的后端包可能还停在没有 ``timeout`` 的签名上。``**kwargs`` 算**不**支持——
    和 ``_method_accepts_max_count`` 对可变关键字的处理是反的，不要顺手统一。
    这里只接 ``ValueError`` 和 ``TypeError``，不接 ``AttributeError``。
    """
    try:
        signature = inspect.signature(cls.execute)
    except (ValueError, TypeError):
        logger.warning(
            "Could not inspect signature of %s.execute; treating timeout as unsupported",
            cls,
            exc_info=True,
        )
        return False
    else:
        return "timeout" in signature.parameters
