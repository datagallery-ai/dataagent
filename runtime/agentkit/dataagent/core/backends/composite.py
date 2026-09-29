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
"""按路径前缀把一次文件操作交给某一个后端。

这个类自己不保存文件字节。它做的事只有四件：选后端、把路径改写成那个后端
眼里的路径、调用它、再把结果里的路径改回调用方原来的写法。

几条和「看起来更合理」相反、但必须留着的行为：

- 执行命令不看路径，也不看路由表里有没有沙箱，只看 ``default``。
- ``grep`` 的条数上限是所有后端合在一起算的。预算在调用下一家之前就用完时，
  不再调用它，并且 ``truncated`` 为真——哪怕那一家其实一条匹配都不会贡献。
- ``glob`` 只有在根上搜索时才改写 pattern。带前导 ``/``、又对不上任何路由前缀的
  pattern 不会进路由后端。命中单条路由时 pattern 原样下发。
- ``delete`` 在本类上被覆写，删除工具不会因为「协议默认没实现删除」被拿掉。
  真正不支持删除的是某条路由指向的子后端。
"""

from . import protocol as contracts
from .state import StateBackend

# 这四个名字必须出现在模块全局里：oracle 按名字核对类对象是不是我们的，
# 差分脚本也是按这个名字把能力探测换成计数器。
SandboxBackendProtocol = contracts.SandboxBackendProtocol
_apply_grep_max_count = contracts._apply_grep_max_count
_method_accepts_max_count = contracts._method_accepts_max_count
execute_accepts_timeout = contracts.execute_accepts_timeout

# 删除工具把这段字原样放进 ToolMessage。插进去的是调用方路径，不是剥过前缀的路径。
_UNSUPPORTED_DELETE = "Error: deletion is not supported for '{file_path}'."

_FANOUT = "fanout"
_ROUTED = "routed"
_PLAIN = "plain"


def _route_for_path(
    *,
    default: contracts.BackendProtocol,
    sorted_routes: list[tuple[str, contracts.BackendProtocol]],
    path: str,
) -> tuple[contracts.BackendProtocol, str, str | None]:
    """按传入列表的顺序，第一次命中就返回。这里不再排序。

    返回 ``(后端, 那个后端眼里的路径, 路由表里的原前缀)``。都没命中时第三项是
    ``None``，路径保持原样，后端就是 ``default`` 那个对象。

    精确比较用的是剥掉全部尾斜杠之后的前缀；前缀匹配才会在末尾补一个边界斜杠。
    ``/memories///`` 因此对得上 ``/memories``，对不上 ``/memories/``。空前缀和
    ``/`` 会吞掉一切绝对路径。大小写、重复斜杠、``..`` 都不折叠。
    """
    for route_prefix, backend in sorted_routes:
        bare = route_prefix.rstrip("/")
        if path == bare:
            return backend, "/", route_prefix
        boundary = route_prefix if route_prefix.endswith("/") else route_prefix + "/"
        if not path.startswith(boundary):
            continue
        suffix = path[len(boundary) :]
        child_path = "/" + suffix if suffix else "/"
        return backend, child_path, route_prefix
    return default, path, None


def _public_path(route_prefix: str, info: dict) -> dict:
    """浅拷贝一条结果，只把 ``path`` 拼回调用方的坐标系。

    公式是 ``前缀去掉最后一个字符 + 子路径``。前缀以 ``/`` 结尾时，去掉的就是
    那个尾斜杠。前缀没有尾斜杠时，最后一个普通字符也会被吃掉：``/memories``
    加上 ``/file.txt`` 得到 ``/memorie/file.txt``。这不是笔误，不要改成「只剥斜杠」。
    """
    copied = dict(info)
    copied["path"] = route_prefix[:-1] + info["path"]
    return copied


def _path_key(info: dict) -> str:
    return info.get("path", "")


def _directory_row(route_prefix: str) -> dict:
    return {"path": route_prefix, "is_dir": True, "size": 0, "modified_at": ""}


def _as_listing(raw):
    if isinstance(raw, contracts.LsResult):
        return raw
    return contracts.LsResult(entries=raw)


def _present_listing(path, raw, route_prefix, longest_first):
    """路由命中就回拼；只有落在默认后端上的 ``/`` 才补虚拟目录。"""
    listing = _as_listing(raw)
    if route_prefix is not None:
        if listing.error:
            return listing
        entries = [_public_path(route_prefix, item) for item in (listing.entries or [])]
        return contracts.LsResult(entries=entries)
    if path != "/":
        return listing
    if listing.error:
        return listing
    entries = list(listing.entries or [])
    entries.extend(_directory_row(prefix) for prefix, _backend in longest_first)
    entries.sort(key=_path_key)
    return contracts.LsResult(entries=entries)


def _as_search(raw):
    if isinstance(raw, contracts.GrepResult):
        return raw
    if isinstance(raw, str):
        return contracts.GrepResult(error=raw)
    return contracts.GrepResult(matches=raw)


def _call_search(backend, method_name, pattern, path, file_glob, max_count):
    """探测为真才把 ``max_count`` 当关键字传下去，避免老签名 ``TypeError``。

    探测看的是这次真正要调的方法名：同步是 ``grep``，异步是 ``agrep``。
    一个类可以只在其中一个上接受这个参数。
    """
    method = getattr(backend, method_name)
    if _method_accepts_max_count(type(backend), method_name):
        return method(pattern, path, file_glob, max_count=max_count)
    return method(pattern, path, file_glob)


def _bounded_search(backend, method_name, pattern, path, file_glob, max_count):
    raw = _call_search(backend, method_name, pattern, path, file_glob, max_count)
    return _apply_grep_max_count(_as_search(raw), max_count)


def _publish_grep(route_prefix, found):
    if found.error:
        return found
    hits = [_public_path(route_prefix, item) for item in (found.matches or [])]
    return contracts.GrepResult(matches=hits, truncated=found.truncated)


def _remaining_budget(max_count, collected):
    if max_count is None:
        return None
    return max(max_count - len(collected), 0)


def _clip_matches(max_count, gathered, truncated):
    """每家已经按剩余额度裁过之后，这道裁剪到不了。留着当第二道保险。"""
    if max_count is not None and len(gathered) > max_count:
        return gathered[:max_count], True
    return gathered, truncated


def _glob_parts(result):
    """返回 ``(matches, truncated, reason, error)``。旧式 list 当作没截断。"""
    if isinstance(result, contracts.GlobResult):
        return result.matches or [], result.truncated, result.truncation_reason, result.error
    return list(result or []), False, None, None


def _prefer_reason(current, incoming):
    """``unreadable`` 从哪一边来都赢；其余原因先到的留下。没有第三级优先级。"""
    if current == "unreadable" or incoming == "unreadable":
        return "unreadable"
    return current or incoming


def _publish_glob(route_prefix, result):
    matches, truncated, reason, error = _glob_parts(result)
    if error:
        return result
    rewritten = [_public_path(route_prefix, item) for item in matches]
    return contracts.GlobResult(matches=rewritten, truncated=truncated, truncation_reason=reason)


def _merge_globs(default_result, routed):
    matches, truncated, reason, error = _glob_parts(default_result)
    if error:
        return default_result
    merged = list(matches)
    for route_prefix, result in routed:
        route_matches, route_truncated, route_reason, route_error = _glob_parts(result)
        if route_error:
            return result
        truncated = truncated or route_truncated
        reason = _prefer_reason(reason, route_reason)
        merged.extend(_public_path(route_prefix, item) for item in route_matches)
    merged.sort(key=_path_key)
    return contracts.GlobResult(matches=merged, truncated=truncated, truncation_reason=reason)


def _pattern_for_route(pattern, route_prefix):
    """根上的 glob 才走这里。单路由那一支不改写 pattern。

    带前导 ``/``、又没有命中这条前缀的 pattern 返回 ``None``，调用方跳过、不调用。
    """
    bare_pattern = pattern.lstrip("/")
    bare_prefix = route_prefix.strip("/") + "/"
    rewritten = "/" + bare_pattern[len(bare_prefix) :] if bare_pattern.startswith(bare_prefix) else pattern
    if rewritten == pattern and pattern.startswith("/"):
        return None
    return rewritten


def _stamp_path(result, original):
    """``path is not None`` 才回写。空字符串要改成调用方路径，``None`` 保持 ``None``。"""
    if result.path is not None:
        result.path = original
    return result


def _batches(staged):
    """按后端对象本身分批，不是按前缀，也不是按 ``id()``。

    批次顺序是该对象第一次出现的顺序，批内是输入顺序。同一个实例既当 default
    又出现在路由里时，文件进同一次调用。
    """
    order = []
    grouped = {}
    for record in staged:
        backend = record[3]
        bucket = grouped.get(backend)
        if bucket is None:
            bucket = []
            grouped[backend] = bucket
            order.append(backend)
        bucket.append(record)
    return [(backend, grouped[backend]) for backend in order]


def _overlay_uploads(files, filled):
    placed = [contracts.FileUploadResponse(path=path, error=None) for path, _content in files]
    for records, responses in filled:
        for record, response in zip(records, responses, strict=False):
            index, original_path = record[0], record[1]
            placed[index] = contracts.FileUploadResponse(path=original_path, error=response.error)
    return placed


def _overlay_downloads(paths, filled):
    placed = [contracts.FileDownloadResponse(path=path, content=None, error=None) for path in paths]
    for records, responses in filled:
        for record, response in zip(records, responses, strict=False):
            index, original_path = record[0], record[1]
            placed[index] = contracts.FileDownloadResponse(
                path=original_path,
                content=response.content,
                error=response.error,
            )
    return placed


def _launch_command(default, command, timeout, method_name):
    """先确认 default 是沙箱，再决定要不要把 timeout 传下去。

    方法要在确认之后才取。非沙箱没有 ``execute``，提前取属性会把
    ``NotImplementedError`` 变成 ``AttributeError``。探测同样放在
    ``isinstance`` 后面：它不吃 ``AttributeError``。``timeout is None``
    写在 ``and`` 左边，这时不去内省签名。
    """
    if isinstance(default, SandboxBackendProtocol):
        launch = getattr(default, method_name)
        if timeout is not None and execute_accepts_timeout(type(default)):
            return launch(command, timeout=timeout)
        return launch(command)
    raise NotImplementedError(
        "Default backend doesn't support command execution (SandboxBackendProtocol). "
        "To enable execution, provide a default backend that implements SandboxBackendProtocol."
    )


class CompositeBackend(contracts.BackendProtocol):
    """把多台后端拼成一台。直接基类只有协议，自己不是沙箱。

    ``routes`` 保存调用方传入的那个 dict，之后追加的键全局搜索看得见。
    ``sorted_routes`` 是构造当时按前缀长度排好的快照，路由和 ``ls("/")`` 的
    虚拟目录看它，看不见后来加的键。两套表不要并成一张。
    """

    def __init__(
        self,
        default: contracts.BackendProtocol | StateBackend,
        routes: dict[str, contracts.BackendProtocol],
        *,
        artifacts_root: str = "/",
    ) -> None:
        self.default = default
        self.routes = routes
        self.sorted_routes = sorted(routes.items(), key=lambda item: len(item[0]), reverse=True)
        self.artifacts_root = artifacts_root

    def _get_backend_and_key(self, key: str) -> tuple[contracts.BackendProtocol, str]:
        backend, stripped, _route_prefix = _route_for_path(
            default=self.default,
            sorted_routes=self.sorted_routes,
            path=key,
        )
        return backend, stripped

    def _where(self, path):
        """``None`` 不路由。命中前缀、根上的合并、落在默认后端，三种。"""
        if path is None:
            return _FANOUT, None, None, None
        backend, child_path, route_prefix = _route_for_path(
            default=self.default,
            sorted_routes=self.sorted_routes,
            path=path,
        )
        if route_prefix is not None:
            return _ROUTED, backend, child_path, route_prefix
        if path == "/":
            return _FANOUT, self.default, path, None
        return _PLAIN, self.default, path, None

    def _stage(self, pairs):
        staged = []
        for index, (original_path, payload) in enumerate(pairs):
            backend, stripped = self._get_backend_and_key(original_path)
            staged.append((index, original_path, stripped, backend, payload))
        return _batches(staged)

    def execute(self, command: str, *, timeout: int | None = None) -> contracts.ExecuteResponse:
        """不按路径挑后端。路由表里的沙箱不算数。"""
        return _launch_command(self.default, command, timeout, "execute")

    async def aexecute(self, command: str, *, timeout: int | None = None) -> contracts.ExecuteResponse:
        """把关键字交给子后端的 ``aexecute``。只覆写了同步 ``execute`` 的子类，靠协议默认实现再转一次。"""
        return await _launch_command(self.default, command, timeout, "aexecute")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[contracts.FileUploadResponse]:
        if not files:
            return []
        filled = []
        for backend, records in self._stage(files):
            batch = [(record[2], record[4]) for record in records]
            filled.append((records, backend.upload_files(batch)))
        return _overlay_uploads(files, filled)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[contracts.FileUploadResponse]:
        if not files:
            return []
        filled = []
        for backend, records in self._stage(files):
            batch = [(record[2], record[4]) for record in records]
            filled.append((records, await backend.aupload_files(batch)))
        return _overlay_uploads(files, filled)

    def download_files(self, paths: list[str]) -> list[contracts.FileDownloadResponse]:
        if not paths:
            return []
        filled = []
        for backend, records in self._stage((path, None) for path in paths):
            batch = [record[2] for record in records]
            filled.append((records, backend.download_files(batch)))
        return _overlay_downloads(paths, filled)

    async def adownload_files(self, paths: list[str]) -> list[contracts.FileDownloadResponse]:
        if not paths:
            return []
        filled = []
        for backend, records in self._stage((path, None) for path in paths):
            batch = [record[2] for record in records]
            filled.append((records, await backend.adownload_files(batch)))
        return _overlay_downloads(paths, filled)

    def delete(self, file_path: str) -> contracts.DeleteResult:
        """子后端抛 ``NotImplementedError`` 时收成结果。别的异常继续往外走。"""
        backend, stripped = self._get_backend_and_key(file_path)
        try:
            result = backend.delete(stripped)
        except NotImplementedError:
            return contracts.DeleteResult(error=_UNSUPPORTED_DELETE.format(file_path=file_path))
        return _stamp_path(result, file_path)

    async def adelete(self, file_path: str) -> contracts.DeleteResult:
        backend, stripped = self._get_backend_and_key(file_path)
        try:
            result = await backend.adelete(stripped)
        except NotImplementedError:
            return contracts.DeleteResult(error=_UNSUPPORTED_DELETE.format(file_path=file_path))
        return _stamp_path(result, file_path)

    def write(self, file_path: str, content: str) -> contracts.WriteResult:
        backend, stripped = self._get_backend_and_key(file_path)
        return _stamp_path(backend.write(stripped, content), file_path)

    async def awrite(self, file_path: str, content: str) -> contracts.WriteResult:
        backend, stripped = self._get_backend_and_key(file_path)
        return _stamp_path(await backend.awrite(stripped, content), file_path)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> contracts.EditResult:
        backend, stripped = self._get_backend_and_key(file_path)
        result = backend.edit(stripped, old_string, new_string, replace_all=replace_all)
        return _stamp_path(result, file_path)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> contracts.EditResult:
        backend, stripped = self._get_backend_and_key(file_path)
        result = await backend.aedit(stripped, old_string, new_string, replace_all=replace_all)
        return _stamp_path(result, file_path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> contracts.ReadResult:
        backend, stripped = self._get_backend_and_key(file_path)
        return backend.read(stripped, offset=offset, limit=limit)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> contracts.ReadResult:
        backend, stripped = self._get_backend_and_key(file_path)
        return await backend.aread(stripped, offset=offset, limit=limit)

    def _fanout_grep(self, pattern, path, file_glob, max_count):
        """沿 ``routes`` 的插入顺序合并。不看 ``sorted_routes``，结果也不排序。"""
        gathered = []
        truncated = False
        head = _bounded_search(self.default, "grep", pattern, path, file_glob, max_count)
        if head.error:
            return head
        gathered.extend(head.matches or [])
        truncated = truncated or head.truncated
        for route_prefix, backend in self.routes.items():
            remaining = _remaining_budget(max_count, gathered)
            if remaining == 0:
                truncated = True
                break
            found = _bounded_search(backend, "grep", pattern, "/", file_glob, remaining)
            if found.error:
                return found
            gathered.extend(_public_path(route_prefix, item) for item in (found.matches or []))
            truncated = truncated or found.truncated
        gathered, truncated = _clip_matches(max_count, gathered, truncated)
        return contracts.GrepResult(matches=gathered, truncated=truncated)

    async def _fanout_agrep(self, pattern, path, file_glob, max_count):
        gathered = []
        truncated = False
        head = _apply_grep_max_count(
            _as_search(await _call_search(self.default, "agrep", pattern, path, file_glob, max_count)),
            max_count,
        )
        if head.error:
            return head
        gathered.extend(head.matches or [])
        truncated = truncated or head.truncated
        for route_prefix, backend in self.routes.items():
            remaining = _remaining_budget(max_count, gathered)
            if remaining == 0:
                truncated = True
                break
            found = _apply_grep_max_count(
                _as_search(await _call_search(backend, "agrep", pattern, "/", file_glob, remaining)),
                remaining,
            )
            if found.error:
                return found
            gathered.extend(_public_path(route_prefix, item) for item in (found.matches or []))
            truncated = truncated or found.truncated
        gathered, truncated = _clip_matches(max_count, gathered, truncated)
        return contracts.GrepResult(matches=gathered, truncated=truncated)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> contracts.GrepResult:
        """上限是全局的。提前停掉的路由仍把 ``truncated`` 标成真。"""
        kind, backend, child_path, route_prefix = self._where(path)
        if kind == _ROUTED:
            found = _bounded_search(backend, "grep", pattern, child_path, glob, max_count)
            return _publish_grep(route_prefix, found)
        if kind == _FANOUT:
            return self._fanout_grep(pattern, path, glob, max_count)
        return _bounded_search(self.default, "grep", pattern, path, glob, max_count)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> contracts.GrepResult:
        """调用子后端的 ``agrep``，不把同步 ``grep`` 丢进线程。"""
        kind, backend, child_path, route_prefix = self._where(path)
        if kind == _ROUTED:
            found = _apply_grep_max_count(
                _as_search(await _call_search(backend, "agrep", pattern, child_path, glob, max_count)),
                max_count,
            )
            return _publish_grep(route_prefix, found)
        if kind == _FANOUT:
            return await self._fanout_agrep(pattern, path, glob, max_count)
        found = _apply_grep_max_count(
            _as_search(await _call_search(self.default, "agrep", pattern, path, glob, max_count)),
            max_count,
        )
        return found

    def _fanout_glob(self, pattern, path):
        head = self.default.glob(pattern, path)
        _matches, _truncated, _reason, error = _glob_parts(head)
        if error:
            return _merge_globs(head, ())
        routed = []
        for route_prefix, backend in self.routes.items():
            rewritten = _pattern_for_route(pattern, route_prefix)
            if rewritten is None:
                continue
            found = backend.glob(rewritten, "/")
            routed.append((route_prefix, found))
            _route_matches, _route_truncated, _route_reason, route_error = _glob_parts(found)
            if route_error:
                return _merge_globs(head, routed)
        return _merge_globs(head, routed)

    async def _fanout_aglob(self, pattern, path):
        head = await self.default.aglob(pattern, path)
        _matches, _truncated, _reason, error = _glob_parts(head)
        if error:
            return _merge_globs(head, ())
        routed = []
        for route_prefix, backend in self.routes.items():
            rewritten = _pattern_for_route(pattern, route_prefix)
            if rewritten is None:
                continue
            found = await backend.aglob(rewritten, "/")
            routed.append((route_prefix, found))
            _route_matches, _route_truncated, _route_reason, route_error = _glob_parts(found)
            if route_error:
                return _merge_globs(head, routed)
        return _merge_globs(head, routed)

    def glob(self, pattern: str, path: str | None = None) -> contracts.GlobResult:
        """单路由不改写 pattern。根上的合并会排序，单路由保持子后端的顺序。"""
        kind, backend, child_path, route_prefix = self._where(path)
        if kind == _ROUTED:
            return _publish_glob(route_prefix, backend.glob(pattern, child_path))
        if kind == _FANOUT:
            return self._fanout_glob(pattern, path)
        return self.default.glob(pattern, path)

    async def aglob(self, pattern: str, path: str | None = None) -> contracts.GlobResult:
        kind, backend, child_path, route_prefix = self._where(path)
        if kind == _ROUTED:
            return _publish_glob(route_prefix, await backend.aglob(pattern, child_path))
        if kind == _FANOUT:
            return await self._fanout_aglob(pattern, path)
        return await self.default.aglob(pattern, path)

    def ls(self, path: str) -> contracts.LsResult:
        backend, child_path, route_prefix = _route_for_path(
            default=self.default,
            sorted_routes=self.sorted_routes,
            path=path,
        )
        return _present_listing(path, backend.ls(child_path), route_prefix, self.sorted_routes)

    async def als(self, path: str) -> contracts.LsResult:
        backend, child_path, route_prefix = _route_for_path(
            default=self.default,
            sorted_routes=self.sorted_routes,
            path=path,
        )
        raw = await backend.als(child_path)
        return _present_listing(path, raw, route_prefix, self.sorted_routes)
