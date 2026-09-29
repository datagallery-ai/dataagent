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
"""把文件放进 LangGraph 的 ``files`` 通道。

这个后端自己不做路径、glob、切片或替换。那些都在 ``utils``。这里只负责：
从当前 superstep 把通道读出来，调一个已有函数，再把变化排进下一次写。

实例上不留任何状态。同一次调用里先写后读要能看见自己刚写的内容，靠的是
读回调的 ``fresh=True``，不是缓存在对象上。
"""

import base64
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph._internal._constants import CONFIG_KEY_READ, CONFIG_KEY_SEND
from langgraph.config import get_config

from . import protocol as contracts
from . import utils as shared

_Channel = dict[str, Any]

# 图外和缺读回调时抛出的原文。相邻字面量与上游折叠成同一句。
_OUTSIDE_GRAPH = (
    "StateBackend must be used inside a LangGraph graph execution "
    "(e.g. via create_deep_agent). It cannot read or write state "
    "outside of a graph context. To pre-populate files, pass them "
    'on invoke: agent.invoke({"messages": [...], "files": {...}})'
)
_MISSING_READ_KEY = (
    "StateBackend requires CONFIG_KEY_READ / CONFIG_KEY_SEND in "
    "the LangGraph config. Make sure the backend is used inside "
    "a graph node or tool, not called directly. To pre-populate "
    "files, pass them on invoke: "
    'agent.invoke({"messages": [...], "files": {...}})'
)


def _child_of(root: str, key: str) -> tuple[str, str] | None:
    """``key`` 在 ``root`` 下是直接文件，还是更深一层里的某个子目录。

    ``root`` 必须已经带尾斜杠。不在这棵子树里返回 ``None``。
    """
    if not key.startswith(root):
        return None
    relative = key[len(root) :]
    if "/" not in relative:
        return ("file", key)
    head, _rest = relative.split("/", 1)
    return ("dir", f"{root}{head}/")


def _plain_file(path: str, size: int, modified_at: str) -> contracts.FileInfo:
    """一条普通文件。``glob`` 与 ``ls`` 的文件行都是这个形状，目录行不是。"""
    return contracts.FileInfo(path=path, is_dir=False, size=size, modified_at=modified_at)


def _measure(data: Any) -> tuple[int, str]:
    """给一条已确认存在的文件数据量长度和修改时间。缺时间戳时用空串，不用 ``None``。"""
    text = shared.file_data_to_string(data)
    stamp = data.get("modified_at", "")
    return len(text), stamp


def _glob_row(files: _Channel, path: str) -> contracts.FileInfo:
    """``glob`` 的一行。``files.get`` 的结果按真值判断：假值时 size 为 0、时间为空串。"""
    data = files.get(path)
    if data:
        size, stamp = _measure(data)
    else:
        size, stamp = 0, ""
    return _plain_file(path, size, stamp)


def _by_path(entry: contracts.FileInfo) -> str:
    return entry.get("path", "")


class StateBackend(contracts.BackendProtocol):
    """默认后端：文件就是图 state 里名为 ``files`` 的那张表。

    键是绝对路径，应当以 ``/`` 开头。本类不做路径规范化，调用方传什么键就用什么键。
    ``read`` 返回的是未加行号的原文；``delete`` 按「精确键 + ``path/`` 前缀」递归删；
    ``glob`` 给出的 ``FileInfo.path`` 就是状态里的绝对路径。

    九个异步方法不在这里覆写，走基类丢进线程的默认实现。``get_config`` 读的是
    contextvar，线程里仍然拿得到当前图的 config。
    """

    def __init__(self) -> None:
        """不接收参数，也不在实例上放任何东西。"""

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> contracts.GrepResult:
        """在内存里做字面量子串搜索。``path is None`` 时搜索根当作 ``/``。

        ``max_count`` 是仅关键字参数，会原样下推。二进制后缀不会被排除。
        """
        files = self._read_files()
        root = "/" if path is None else path
        return shared.grep_matches_from_files(files, pattern, root, glob, max_count=max_count)

    def glob(self, pattern: str, path: str | None = None) -> contracts.GlobResult:
        """把内存搜索的结果装成 ``FileInfo`` 列表。

        ``path`` 原样交给搜索函数，可以是 ``None``。模式被拒时只填 ``error``，
        ``matches`` 保持 ``None``；一个都没命中时 ``matches`` 是空列表。
        顺序沿用搜索函数给出的 ``modified_at`` 倒序，这里不再排。
        通配符语义（含 ``[!abc]`` 与 ``{a,b}``）在共用的匹配器里，不在这里实现。
        """
        files = self._read_files()
        try:
            found = shared._glob_search_files(files, pattern, path)
        except shared.InvalidGlobPatternError as exc:
            return contracts.GlobResult(error=str(exc))
        if found == "No files found":
            return contracts.GlobResult(matches=[])
        paths = found.split("\n")
        return contracts.GlobResult(matches=[_glob_row(files, one) for one in paths])

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> contracts.ReadResult:
        """读一个窗口的原文。协议层 ``limit`` 默认 2000。

        先看键在不在，再按路径后缀决定是不是当全文二进制返回，最后才切片。
        不存在时的 ``error`` **没有** ``Error:`` 前缀——工具层会自己补。
        负 offset 与 ``limit <= 0`` 交给 ``slice_read_response``，这里不再钳一次。
        """
        files = self._read_files()
        file_data = files.get(file_path)
        if file_data is None:
            return contracts.ReadResult(error=f"File '{file_path}' not found")
        if shared._get_backend_read_file_type(file_path) != "text":
            text = shared.file_data_to_string(file_data)
            copied = shared._copy_file_data_with_content(file_data, text)
            return contracts.ReadResult(file_data=copied)
        return shared.slice_read_response(file_data, offset, limit)

    def write(self, file_path: str, content: str) -> contracts.WriteResult:
        """不存在就新建，存在就整份覆盖。永远不返回 ``error``。

        判据是 ``is not None``，不是真值。通道里若存着 ``{}`` 这样的假值，
        这里仍走更新，从而继承那份数据里已有的字段。
        """
        files = self._read_files()
        existing = files.get(file_path)
        fresh = shared.update_file_data(existing, content) if existing is not None else shared.create_file_data(content)
        self._send_files_update({file_path: self._prepare_for_storage(fresh)})
        return contracts.WriteResult(path=file_path)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> contracts.EditResult:
        """精确替换。``old_string`` 出现多次且 ``replace_all`` 为假时，整次编辑失败。

        不要求 ``new_string`` 与 ``old_string`` 不同：相同且唯一时是一次成功的空操作。
        失败文案原样来自替换函数，这里不加前缀，也不发写。
        """
        files = self._read_files()
        file_data = files.get(file_path)
        if file_data is None:
            return contracts.EditResult(error=f"Error: File '{file_path}' not found")
        content = shared.file_data_to_string(file_data)
        replaced = shared.perform_string_replacement(content, old_string, new_string, replace_all)
        if isinstance(replaced, str):
            return contracts.EditResult(error=replaced)
        new_content, occurrences = replaced
        stored = shared.update_file_data(file_data, new_content)
        self._send_files_update({file_path: self._prepare_for_storage(stored)})
        return contracts.EditResult(path=file_path, occurrences=occurrences)

    def delete(self, file_path: str) -> contracts.DeleteResult:
        """递归删除：精确键，以及所有以 ``键 + "/"`` 为前缀的键。

        尾斜杠会全部剥掉再匹配，返回的 ``path`` 仍是调用方传入的原串。
        ``delete("/")`` 会删掉整棵以 ``/`` 开头的树，这里不加拒绝根目录的守卫。
        """
        files = self._read_files()
        base = file_path.rstrip("/")
        prefix = f"{base}/"
        doomed = [key for key in files if key == base or key.startswith(prefix)]
        if not doomed:
            return contracts.DeleteResult(error=f"Error: File '{file_path}' not found")
        self._send_files_update(dict.fromkeys(doomed, None))
        return contracts.DeleteResult(path=file_path)

    def ls(self, path: str) -> contracts.LsResult:
        """非递归列一层。更深的键折成带尾斜杠的目录行。

        只在路径末尾补一个 ``/``，不做别的规范化。目录不存在、相对路径、
        指向一个文件，都返回空列表，``error`` 保持 ``None``。
        ``size`` 是归一化文本的字符数。文件与目录混在一起按 path 排序。
        """
        files = self._read_files()
        root = path if path.endswith("/") else f"{path}/"
        direct: list[contracts.FileInfo] = []
        folders: set[str] = set()
        for key, data in files.items():
            kind = _child_of(root, key)
            if kind is None:
                continue
            role, located = kind
            if role == "dir":
                folders.add(located)
                continue
            size, stamp = _measure(data)
            direct.append(_plain_file(located, size, stamp))
        folders_as_rows = [contracts.FileInfo(path=folder, is_dir=True, size=0, modified_at="") for folder in sorted(folders)]
        entries = direct + folders_as_rows
        entries.sort(key=_by_path)
        return contracts.LsResult(entries=entries)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[contracts.FileUploadResponse]:
        """批量写入。这个后端上不会失败，返回顺序与入参一致。

        非法 UTF-8 会先 base64 再当普通文本存，**不**把 ``encoding`` 改成 ``"base64"``。
        因此二进制上传后再下载，拿回来的是那段 base64 的 ASCII 字节。
        同一批里同一路径出现两次时，第二次仍按循环开始前读到的旧值决定新建还是更新。
        判据是真值，不是 ``is not None``。空列表一次写都不发。
        """
        stored = self._read_files()
        responses: list[contracts.FileUploadResponse] = []
        pending: _Channel = {}
        for path, payload in files:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                text = base64.b64encode(payload).decode("ascii")
            prev = stored.get(path)
            file_data = shared.update_file_data(prev, text) if prev else shared.create_file_data(text)
            pending[path] = {**file_data}
            responses.append(contracts.FileUploadResponse(path=path, error=None))
        if pending:
            self._send_files_update(pending)
        return responses

    def download_files(self, paths: list[str]) -> list[contracts.FileDownloadResponse]:
        """批量读出字节。缺文件是逐条的 ``file_not_found``，不是异常。

        ``encoding`` 等于 ``utf-8``（或缺失，按 utf-8）才当文本编码；其它一切都按
        base64 解码，解码失败原样抛出。
        """
        state_files = self._read_files()
        responses: list[contracts.FileDownloadResponse] = []
        for path in paths:
            file_data = state_files.get(path)
            if file_data is None:
                missing = contracts.FileDownloadResponse(path=path, content=None, error=contracts.FILE_NOT_FOUND)
                responses.append(missing)
                continue
            text = shared.file_data_to_string(file_data)
            encoding = file_data.get("encoding", "utf-8")
            raw = text.encode("utf-8") if encoding == "utf-8" else base64.standard_b64decode(text)
            responses.append(contracts.FileDownloadResponse(path=path, content=raw, error=None))
        return responses

    def _prepare_for_storage(self, file_data: contracts.FileData) -> dict[str, Any]:
        """浅拷贝一份再放进通道，让通道里的对象和本地变量脱钩。不做格式转换。"""
        return {**file_data}

    def _send_files_update(self, update: dict[str, Any]) -> None:
        """排一次部分更新：一个列表，里面只有 ``("files", update)`` 这一对。"""
        config = self._get_config()
        sender = config["configurable"][CONFIG_KEY_SEND]
        sender([("files", update)])

    def _read_files(self) -> dict[str, Any]:
        """读当前 ``files``。``fresh=True`` 把本 superstep 里还没提交的写算进去。

        通道还是空的时候回调返回 ``None``，这里收成 ``{}``。返回的字典只读，不就地改。
        """
        config = self._get_config()
        reader = config["configurable"][CONFIG_KEY_READ]
        return reader("files", True) or {}

    def _get_config(self) -> RunnableConfig:
        """取出当前图的 config。只检查读回调在不在，不检查写回调。"""
        try:
            config = get_config()
        except RuntimeError:
            raise RuntimeError(_OUTSIDE_GRAPH) from None
        configurable = config.get("configurable", {})
        if CONFIG_KEY_READ not in configurable:
            raise RuntimeError(_MISSING_READ_KEY)
        return config
