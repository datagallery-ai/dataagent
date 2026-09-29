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
"""把文件放进 LangGraph 的 ``BaseStore``。

路径折叠、读窗口、替换、grep、glob 都不在这里。本类只做四件事：解析调用方
给的 namespace 工厂、把 ``Item.value`` 收成 ``FileData``、按页把 namespace
下的条目拉完，再把结果交给 ``utils``。

构造时不调用工厂，也不碰 store。每次公开方法都重新取 store、重新调用工厂。
``read`` 找不到文件时不加 ``Error: `` 前缀；``edit`` 和 ``delete`` 加。
``ls`` / ``grep`` / ``glob`` 只跳过 ``ValueError``，``TypeError`` 继续抛。
"""

import base64
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langgraph.config import get_store
from langgraph.runtime import get_runtime
from langgraph.store.base import BaseStore, Item, PutOp

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

from . import protocol as contracts
from . import utils as shared

# 引号必须留着：``Runtime`` 只在类型检查时存在，这条赋值在 import 时就会求值。
NamespaceFactory = Callable[["Runtime[Any]"], tuple[str, ...]]

_NAMESPACE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9\-_.@+:~]+$")

# 两条都不是 f-string。措辞是给直接调用方看的，测试只锁其中一段子串。
_MISSING_STORE_MESSAGE = (
    "StoreBackend must be used inside a LangGraph graph execution (e.g. via create_deep_agent), "
    "or initialized with an explicit store and namespace: "
    "StoreBackend(store=my_store, namespace=lambda _rt: ('filesystem',))"
)
_NAMESPACE_RUNTIME_MESSAGE = (
    "The namespace factory tried to read the Runtime, but it is unavailable (running outside a LangGraph graph execution). "
    "Use StoreBackend inside a graph (e.g. via create_deep_agent), or pass a namespace factory that does not read the Runtime."
)

_NO_GLOB_HITS = "No files found"


def _validate_namespace(namespace: tuple[str, ...]) -> tuple[str, ...]:
    """原样返回传入的容器。空容器、空组件、非法字符各有一句，类型错误是 ``TypeError``。

    先看真值，不要求它是 tuple。非空 list 会通过这里，随后在 ``store.put`` 里才因为
    不可哈希失败。组件先查类型再查空串：``0`` 是 ``TypeError``，不是「不能为空」。
    """
    if not namespace:
        raise ValueError("Namespace tuple must not be empty.")
    for i, component in enumerate(namespace):
        if not isinstance(component, str):
            raise TypeError(f"Namespace component at index {i} must be a string, got {type(component).__name__}.")
        if not component:
            raise ValueError(f"Namespace component at index {i} must not be empty.")
        if _NAMESPACE_COMPONENT_RE.match(component) is None:
            raise ValueError(
                f"Namespace component at index {i} contains disallowed characters: {component!r}. "
                f"Only alphanumeric characters, hyphens, underscores, dots, @, +, colons, and tildes are allowed."
            )
    return namespace


def _gather_pages(fetch: Callable[[int, int], list[Item] | None], page_size: int) -> list[Item]:
    """把一页页结果拼起来。偏移每次加 ``page_size``，不是这一页的实际长度。

    空页用真值判断，所以 ``None`` 当成没有更多数据，而不是 ``TypeError``。
    页的长度刚好等于 ``page_size`` 时还要再拉一次。``page_size`` 为 0 且 store
    仍返回非空页时，偏移停在 0，循环不会自己停。
    """
    gathered: list[Item] = []
    offset = 0
    while True:
        page = fetch(page_size, offset)
        if not page:
            break
        gathered.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return gathered


def _upload_text(payload: bytes) -> tuple[str, str]:
    """能当 UTF-8 就存原文，否则存 standard base64 的 ASCII。只接解码错误。"""
    try:
        return payload.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        encoded = base64.standard_b64encode(payload).decode("ascii")
        return encoded, "base64"


def _child_location(prefix: str, key: str) -> tuple[str, str] | None:
    """``key`` 在 ``prefix`` 下是直接文件，还是更深一层的子目录。不在这棵树里返回 ``None``。

    ``prefix`` 必须已经带尾斜杠。目录名是 ``relative.split('/')[0]``，所以
    ``/dir//x`` 会得到目录 ``/dir//``。
    """
    if not key.startswith(prefix):
        return None
    relative = key[len(prefix) :]
    if "/" not in relative:
        return ("file", key)
    head = relative.split("/")[0]
    return ("directory", prefix + head + "/")


def _glob_row(files: dict[Any, contracts.FileData], path: str) -> contracts.FileInfo:
    """glob 的一行。``files.get`` 按真值判断：假值时长度为 0、时间为空串。永远不是目录。"""
    data = files.get(path)
    if data:
        size = len(shared.file_data_to_string(data))
        stamp = data.get("modified_at", "")
    else:
        size, stamp = 0, ""
    return contracts.FileInfo(path=path, is_dir=False, size=int(size), modified_at=stamp)


class StoreBackend(contracts.BackendProtocol):
    """文件就是 store 里的一条 ``Item``。namespace 由工厂每次现算。

    直接基类只有 ``BackendProtocol``。这里没有 ``execute``：调用它是
    ``AttributeError``，不是沙箱协议上的 ``NotImplementedError``。

    ``als`` / ``agrep`` / ``aglob`` / ``aupload_files`` / ``adownload_files``
    不在本类里，走基类丢进线程的实现。``agrep`` 因此仍有 35 秒超时。
    ``aread`` / ``awrite`` / ``aedit`` / ``adelete`` 打到 ``aget`` / ``aput`` /
    ``asearch`` / ``abatch``，不是同步方法的线程包装。
    """

    def __init__(self, *, namespace: NamespaceFactory, store: BaseStore | None = None) -> None:
        """两个参数都是仅关键字。``namespace`` 必填，``store`` 可以留到图里再取。

        这里不调用工厂。非法 namespace、会抛错的工厂，都要等到第一次真正读写。
        """
        self.namespace = namespace
        self.store = store

    def _get_store(self) -> BaseStore:
        """显式传入的 store 优先，包括以后被换成假对象的情况。判据是 ``is not None``。

        ``get_store()`` 的 ``RuntimeError`` 和 ``KeyError`` 换成缺 store 那句，
        并且 ``from None``。``ValueError``、``AttributeError`` 原样出去。
        成功拿到 ``None`` 时就返回 ``None``，下一步 ``.get`` 才变成 ``AttributeError``。
        """
        if self.store is not None:
            return self.store
        try:
            return get_store()
        except (RuntimeError, KeyError):
            raise RuntimeError(_MISSING_STORE_MESSAGE) from None

    def _get_namespace(self) -> tuple[str, ...]:
        """先取 runtime，再调用工厂。runtime 取不到时工厂仍会被调用，参数是 ``None``。

        工厂在 ``runtime is None`` 时抛的 ``AttributeError`` 换成另一句
        ``RuntimeError``，``__cause__`` 留着原来的异常。runtime 是真对象时，
        同样的 ``AttributeError`` 不包装。``KeyError`` 和 ``TypeError`` 也不包装。
        """
        try:
            runtime = get_runtime()
        except (RuntimeError, KeyError):
            runtime = None
        try:
            namespace = self.namespace(runtime)
        except AttributeError as exc:
            if runtime is None:
                raise RuntimeError(_NAMESPACE_RUNTIME_MESSAGE) from exc
            raise
        return _validate_namespace(namespace)

    def _open(self) -> tuple[BaseStore, tuple[str, ...]]:
        """先 store，后 namespace。两边都坏时，调用方看到的是缺 store，工厂次数是 0。"""
        store = self._get_store()
        namespace = self._get_namespace()
        return store, namespace

    def _convert_store_item_to_file_data(self, store_item: Item) -> contracts.FileData:
        """把 ``Item.value`` 收成 ``FileData``。不读 ``Item`` 自己的时间戳。

        缺 ``content`` 和显式 ``None`` 都是 ``ValueError``。legacy ``list[str]``
        用换行拼起来，不改原来的 list。混进非字符串时，``TypeError`` 的类型名是
        整个 ``content`` 的类型，所以消息以 ``got list`` 结尾。
        时间戳键在、并且值是 ``str``，才抄进结果。``""`` 算，``int`` 不算。
        """
        raw_content = store_item.value.get("content")
        if raw_content is None:
            raise ValueError(f"Store item does not contain valid content field. Got: {store_item.value.keys()}")
        if isinstance(raw_content, list) and all(isinstance(piece, str) for piece in raw_content):
            content = "\n".join(raw_content)
        elif isinstance(raw_content, str):
            content = raw_content
        else:
            raise TypeError(
                f"Store item `content` must be a `str` or legacy `list[str]`, got {type(raw_content).__name__}."
            )
        file_data = contracts.FileData(content=content, encoding=store_item.value.get("encoding", "utf-8"))
        for stamp in ("created_at", "modified_at"):
            if stamp in store_item.value and isinstance(store_item.value[stamp], str):
                file_data[stamp] = store_item.value[stamp]
        return file_data

    def _convert_file_data_to_store_value(self, file_data: contracts.FileData) -> dict[str, Any]:
        """写回 store 的 value。时间戳有键就照抄，不看是不是 ``str``。未知键丢掉。

        键序是 ``content``、``encoding``，然后才是存在的两个时间戳。缺 ``encoding``
        是 ``KeyError``。
        """
        stored = {"content": file_data["content"], "encoding": file_data["encoding"]}
        if "created_at" in file_data:
            stored["created_at"] = file_data["created_at"]
        if "modified_at" in file_data:
            stored["modified_at"] = file_data["modified_at"]
        return stored

    def _search_store_paginated(
        self,
        store: BaseStore,
        namespace: tuple[str, ...],
        *,
        query: str | None = None,
        filter: dict[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[Item]:
        """同步把 namespace 拉完。函数体不读 ``self``，测试会把别的对象放在第一个参数上。

        ``query``、``filter``、``limit``、``offset`` 每次都按关键字传入，包括它们是 ``None``。
        不传 ``refresh_ttl``。
        """

        def fetch(limit: int, offset: int) -> list[Item] | None:
            return store.search(namespace, query=query, filter=filter, limit=limit, offset=offset)

        return _gather_pages(fetch, page_size)

    async def _asearch_store_paginated(
        self,
        store: BaseStore,
        namespace: tuple[str, ...],
        *,
        query: str | None = None,
        filter: dict[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[Item]:
        """与同步分页同一套停法，只是把 ``search`` 换成 ``asearch``。"""
        gathered: list[Item] = []
        offset = 0
        while True:
            page = await store.asearch(namespace, query=query, filter=filter, limit=page_size, offset=offset)
            if not page:
                break
            gathered.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return gathered

    def _index_files(self, items: list[Item]) -> dict[Any, contracts.FileData]:
        """搜索顺序就是插入顺序。``ValueError`` 的条目不进表，``TypeError`` 抛出。"""
        indexed: dict[Any, contracts.FileData] = {}
        for item in items:
            try:
                indexed[item.key] = self._convert_store_item_to_file_data(item)
            except ValueError:
                continue
        return indexed

    def _read_loaded(self, item: Item | None, file_path: str, offset: int, limit: int) -> contracts.ReadResult:
        """条目已经取到之后的短路：不存在、坏内容、非文本、再切片。

        不存在的文件不看后缀。非文本把转换结果原样放进 ``ReadResult``，不切片，
        也不走 ``_copy_file_data_with_content``。``ValueError`` 的文本没有 ``Error: `` 前缀。
        """
        if item is None:
            return contracts.ReadResult(error=f"File '{file_path}' not found")
        try:
            file_data = self._convert_store_item_to_file_data(item)
        except ValueError as e:
            return contracts.ReadResult(error=str(e))
        if shared._get_backend_read_file_type(file_path) != "text":
            return contracts.ReadResult(file_data=file_data)
        return shared.slice_read_response(file_data, offset, limit)

    def _keys_to_drop(self, items: list[Item], file_path: str) -> list[str]:
        """剥掉全部尾斜杠再匹配。命中顺序就是搜索顺序，不排序。"""
        base = file_path.rstrip("/")
        prefix = base + "/"
        return [str(item.key) for item in items if str(item.key) == base or str(item.key).startswith(prefix)]

    def ls(self, path: str) -> contracts.LsResult:
        """只列一层。只在末尾缺斜杠时补一个 ``/``，不做 ``normpath``，不折叠重复斜杠。

        比较用 ``str(key)``，放进 ``FileInfo["path"]`` 的文件路径是 ``item.key`` 原对象。
        坏内容（``ValueError``）跳过；legacy list 里混了非字符串则整次抛 ``TypeError``。
        ``size`` 是字符数。目录行的 ``modified_at`` 固定是空串。全体按 path 排序。
        """
        store, namespace = self._open()
        items = self._search_store_paginated(store, namespace)
        prefix = path if path.endswith("/") else path + "/"
        rows: list[contracts.FileInfo] = []
        folders: set[str] = set()
        for item in items:
            located = _child_location(prefix, str(item.key))
            if located is None:
                continue
            role, where = located
            if role == "directory":
                folders.add(where)
                continue
            try:
                file_data = self._convert_store_item_to_file_data(item)
            except ValueError:
                continue
            text = shared.file_data_to_string(file_data)
            rows.append(
                contracts.FileInfo(
                    path=item.key,
                    is_dir=False,
                    size=len(text),
                    modified_at=file_data.get("modified_at", ""),
                )
            )
        for folder in sorted(folders):
            rows.append(contracts.FileInfo(path=folder, is_dir=True, size=0, modified_at=""))
        rows.sort(key=lambda entry: entry.get("path", ""))
        return contracts.LsResult(entries=rows)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> contracts.ReadResult:
        """同步读。``limit`` 默认 2000。负 offset 和非正 limit 交给切片函数，这里不再判。"""
        store, namespace = self._open()
        item = store.get(namespace, file_path)
        return self._read_loaded(item, file_path, offset, limit)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> contracts.ReadResult:
        """异步读。取条目用 ``aget``，store 和 namespace 的解析仍是同步的。"""
        store, namespace = self._open()
        item = await store.aget(namespace, file_path)
        return self._read_loaded(item, file_path, offset, limit)

    def _compose_write(self, existing: Item | None, content: str) -> contracts.FileData:
        """已有条目走 ``update_file_data``，从而留下 encoding 和 ``created_at``。

        新建走 ``create_file_data``。坏内容在转换时抛出，调用方不会 ``put``。
        """
        if existing is not None:
            current = self._convert_store_item_to_file_data(existing)
            return shared.update_file_data(current, content)
        return shared.create_file_data(content)

    def write(self, file_path: str, content: str) -> contracts.WriteResult:
        """不存在就新建，存在就整份覆盖。没有失败分支，转换或 ``put`` 抛什么就让它抛。"""
        store, namespace = self._open()
        existing = store.get(namespace, file_path)
        file_data = self._compose_write(existing, content)
        store.put(namespace, file_path, self._convert_file_data_to_store_value(file_data))
        return contracts.WriteResult(path=file_path)

    async def awrite(self, file_path: str, content: str) -> contracts.WriteResult:
        """异步写。记录上是 ``aget`` 然后 ``aput``。"""
        store, namespace = self._open()
        existing = await store.aget(namespace, file_path)
        file_data = self._compose_write(existing, content)
        await store.aput(namespace, file_path, self._convert_file_data_to_store_value(file_data))
        return contracts.WriteResult(path=file_path)

    def _apply_edit(
        self,
        item: Item | None,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool,
    ) -> tuple[contracts.EditResult | None, dict[str, Any] | None]:
        """算出编辑结果。失败时第一项是 ``EditResult``，成功时第二项是要写回的 value。"""
        if item is None:
            return contracts.EditResult(error=f"Error: File '{file_path}' not found"), None
        try:
            file_data = self._convert_store_item_to_file_data(item)
        except ValueError as e:
            return contracts.EditResult(error=f"Error: {e}"), None
        replaced = shared.perform_string_replacement(
            shared.file_data_to_string(file_data),
            old_string,
            new_string,
            replace_all,
        )
        if isinstance(replaced, str):
            return contracts.EditResult(error=replaced), None
        new_content, occurrences = replaced
        updated = shared.update_file_data(file_data, new_content)
        outcome = contracts.EditResult(path=file_path, occurrences=int(occurrences))
        return outcome, self._convert_file_data_to_store_value(updated)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> contracts.EditResult:
        """精确替换。替换函数返回的失败字符串原样放进 ``error``，不再加一层 ``Error: ``。

        找不到文件、以及内容转换的 ``ValueError``，则带 ``Error: `` 前缀。
        ``TypeError`` 继续抛。失败不 ``put``。
        """
        store, namespace = self._open()
        item = store.get(namespace, file_path)
        outcome, stored = self._apply_edit(item, file_path, old_string, new_string, replace_all)
        if stored is None:
            return outcome
        store.put(namespace, file_path, stored)
        return outcome

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> contracts.EditResult:
        """异步编辑。成功时 ``aget`` 之后是 ``aput``。"""
        store, namespace = self._open()
        item = await store.aget(namespace, file_path)
        outcome, stored = self._apply_edit(item, file_path, old_string, new_string, replace_all)
        if stored is None:
            return outcome
        await store.aput(namespace, file_path, stored)
        return outcome

    def delete(self, file_path: str) -> contracts.DeleteResult:
        """一次 ``batch`` 删掉精确键和 ``键/`` 前缀下的一切。``PutOp`` 的 value 是 ``None``。

        返回的 ``path`` 和错误消息里的路径都是调用方原来的字符串，含尾斜杠。
        ``delete("/")`` 与 ``delete("")`` 会删掉空键和一切以 ``/`` 开头的键。
        不读内容，坏条目一样删。找不到时不调用 ``batch``。
        """
        store, namespace = self._open()
        items = self._search_store_paginated(store, namespace)
        doomed = self._keys_to_drop(items, file_path)
        if not doomed:
            return contracts.DeleteResult(error=f"Error: File '{file_path}' not found")
        store.batch([PutOp(namespace, key, None) for key in doomed])
        return contracts.DeleteResult(path=file_path)

    async def adelete(self, file_path: str) -> contracts.DeleteResult:
        """异步删除。搜索走 ``asearch``，提交走一次 ``abatch``。"""
        store, namespace = self._open()
        items = await self._asearch_store_paginated(store, namespace)
        doomed = self._keys_to_drop(items, file_path)
        if not doomed:
            return contracts.DeleteResult(error=f"Error: File '{file_path}' not found")
        await store.abatch([PutOp(namespace, key, None) for key in doomed])
        return contracts.DeleteResult(path=file_path)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> contracts.GrepResult:
        """把 namespace 里的文件交给字面量搜索。命中顺序等于搜索插入顺序，这里不排序。

        非法 glob 过滤器的错误句子在 utils 里已经成形，这里不加 ``Error: ``。
        """
        store, namespace = self._open()
        items = self._search_store_paginated(store, namespace)
        files = self._index_files(items)
        return shared.grep_matches_from_files(files, pattern, path, glob, max_count=max_count)

    def glob(self, pattern: str, path: str | None = None) -> contracts.GlobResult:
        """先把条目拉完并转换，然后才搜索。模式非法时 ``search`` 已经发生过。

        只接 ``InvalidGlobPatternError``。普通 ``ValueError`` 穿透，不会被说成模式错误。
        没命中时 ``matches`` 是空列表，哨兵字符串不放进 ``error``。
        """
        store, namespace = self._open()
        items = self._search_store_paginated(store, namespace)
        files = self._index_files(items)
        try:
            found = shared._glob_search_files(files, pattern, path)
        except shared.InvalidGlobPatternError as exc:
            return contracts.GlobResult(error=str(exc))
        if found == _NO_GLOB_HITS:
            return contracts.GlobResult(matches=[])
        rows = [_glob_row(files, one) for one in found.split("\n")]
        return contracts.GlobResult(matches=rows)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[contracts.FileUploadResponse]:
        """按输入顺序逐个 ``put``。每次都新建 ``FileData``，不继承旧的 ``created_at``。

        ``encoding`` 用关键字传入。中途 ``put`` 失败时，前面的文件已经写上，异常冒出。
        空列表也要先解析 store 和 namespace。
        """
        store, namespace = self._open()
        responses: list[contracts.FileUploadResponse] = []
        for path, payload in files:
            content, encoding = _upload_text(payload)
            file_data = shared.create_file_data(content, encoding=encoding)
            store.put(namespace, path, self._convert_file_data_to_store_value(file_data))
            responses.append(contracts.FileUploadResponse(path=path, error=None))
        return responses

    def download_files(self, paths: list[str]) -> list[contracts.FileDownloadResponse]:
        """按输入顺序逐个 ``get``。找不到是这一条的 ``file_not_found``，后面的路径还要处理。

        转换抛错会让整批中断，不会收成 ``file_not_found``，也不返回已经成功的前缀。
        只有 ``encoding == "base64"`` 才解码，``b64decode`` 不用 ``validate=True``。
        ``"BASE64"`` 和 ``"utf-16"`` 都按 UTF-8 编码那串文本。
        """
        store, namespace = self._open()
        responses: list[contracts.FileDownloadResponse] = []
        for path in paths:
            item = store.get(namespace, path)
            if item is None:
                responses.append(contracts.FileDownloadResponse(path=path, content=None, error="file_not_found"))
                continue
            file_data = self._convert_store_item_to_file_data(item)
            content = shared.file_data_to_string(file_data)
            raw = base64.standard_b64decode(content) if file_data["encoding"] == "base64" else content.encode("utf-8")
            responses.append(contracts.FileDownloadResponse(path=path, content=raw, error=None))
        return responses
