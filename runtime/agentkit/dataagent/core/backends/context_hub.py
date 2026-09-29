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
"""把文件放进 LangSmith Hub 的 agent 仓库。

本地只留最近一次快照，再加上还没推上去的变更。50 毫秒内的多次写入合成一次
``push_agent``。冲突再试 3 次，第 4 次仍然冲突就整批失败并丢掉缓存。

几条不要顺手改掉的差别：

- 缺密钥时照样 ``Client()``，不提前跳过。警告和连接错误是客户端自己的。
- ``read`` 找不到文件时没有 ``Error: `` 前缀。``edit`` 和 ``delete`` 有。
- ``grep`` 的 path 是前缀，``foo`` 会命中 ``foobar``。``ls`` 才要求后面跟斜杠。
- ``glob`` 收下 ``path`` 但不用。
- 空字符串是合法文件内容。有没有提交过看哈希是不是 ``None``，不看真值。
- ``client`` 只用 ``is not None`` 判断，不用真值。
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import threading
import time
from urllib.parse import urlsplit

from langsmith import Client
from langsmith.schemas import FileEntry
from langsmith.utils import (
    LangSmithConflictError,
    LangSmithError,
    LangSmithNotFoundError,
    parse_hub_identifier,
)

from . import utils as shared
from .protocol import (
    FILE_NOT_FOUND,
    INVALID_PATH,
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

logger = logging.getLogger(__name__)

_HASH_PATTERN = re.compile(r"[0-9a-f]{8}")
_BATCH_SECONDS = 0.05
_CONFLICT_RETRIES = 3
_THREAD_NAME = "context-hub-mutations"

_CACHE_INIT_FAILED = "Context Hub cache failed to initialize"
_DEADLINE_MISSING = "Context Hub mutation deadline is missing"
_HASH_UNRESOLVED = "Context Hub commit succeeded but its hash could not be resolved"
_RETRY_EXHAUSTED = "Context Hub conflict retry loop exhausted unexpectedly"
_PENDING_STALE = "Context Hub changed before a pending mutation could be applied"


class _WholeFile:
    """整文件写入。``changes`` 与排队对象上的那份 dict 不是同一个。"""

    def __init__(self, changes: dict[str, str | None]) -> None:
        self.changes = changes


class _TextSwap:
    """按原文重放的替换。冲突之后用远程正文再算一次。"""

    def __init__(self, path: str, old_string: str, new_string: str, replace_all: bool) -> None:
        self.path = path
        self.old_string = old_string
        self.new_string = new_string
        self.replace_all = replace_all


class _TreeRemoval:
    """删除一个键以及它下面的所有子路径。``path`` 已经去掉尾部斜杠。"""

    def __init__(self, path: str) -> None:
        self.path = path


class _Queued:
    """一次已经收下的变更，和正在等它落盘的调用方。"""

    def __init__(self, intent: _WholeFile | _TextSwap | _TreeRemoval, changes: dict[str, str | None], occurrences: int | None) -> None:
        self.intent = intent
        self.changes = changes
        self.occurrences = occurrences
        self.done = threading.Event()
        self.error: BaseException | None = None


class _Mailbox:
    """同一把条件锁下面的待发送列表和正在发送的列表。"""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.pending: list[_Queued] = []
        self.in_flight: list[_Queued] = []
        self.deadline: float | None = None


def _strip_slashes(path: str) -> str:
    """只去掉开头的 ``/``。不去尾，也不折叠中间。"""
    return path.lstrip("/")


def _apply_overlay(base: dict[str, str], changes: dict[str, str | None]) -> None:
    """``None`` 是删除。空字符串要留下来。"""
    for stored_path, body in changes.items():
        if body is None:
            base.pop(stored_path, None)
            continue
        base[stored_path] = body


def _merge(batch: list[_Queued]) -> dict[str, str | None]:
    merged: dict[str, str | None] = {}
    for queued in batch:
        merged.update(queued.changes)
    return merged


def _to_payload(changes: dict[str, str | None]) -> dict[str, FileEntry | None]:
    payload: dict[str, FileEntry | None] = {}
    for stored_path, body in changes.items():
        if body is None:
            payload[stored_path] = None
            continue
        payload[stored_path] = FileEntry(type="file", content=body)
    return payload


def _hub_down(exc: BaseException) -> str:
    return f"Hub unavailable: {exc}"


def _grep_prefix(path: str | None) -> str:
    """空 path 没有前缀。非空时去掉首尾斜杠，后面做的是 ``startswith``，不加斜杠。"""
    if not path:
        return ""
    return _strip_slashes(path).rstrip("/")


def _fnmatch_filter(rule: str | None):
    """空规则不编译。``normcase`` 交给 ``os``，不要自己转小写。"""
    if not rule:
        return None
    return re.compile(fnmatch.translate(os.path.normcase(rule)))


def _keep_adding(hits: list, stored_path: str, body: str, pattern: str, max_count: int | None) -> bool:
    """返回真表示已经证明还有更多命中，调用方应带上 ``truncated=True`` 停掉。"""
    for index, line in enumerate(body.splitlines(), start=1):
        if pattern not in line:
            continue
        if max_count is not None and len(hits) >= max_count:
            return True
        hits.append({"path": f"/{stored_path}", "line": index, "text": line})
    return False


def _list_children(snapshot: dict[str, str], hub_prefix: str) -> list:
    """只列下一层。有前缀时必须是 ``前缀/``，所以文件自身和更长的兄弟名不会混进来。"""
    seen_dirs: set[str] = set()
    rows = []
    for stored_path in snapshot:
        if hub_prefix and not stored_path.startswith(hub_prefix + "/"):
            continue
        relative = stored_path[len(hub_prefix) + 1 :] if hub_prefix else stored_path
        if not relative:
            continue
        head, sep, _tail = relative.partition("/")
        if sep == "":
            rows.append({"path": f"/{stored_path}", "is_dir": False})
            continue
        dir_path = f"{hub_prefix}/{head}" if hub_prefix else head
        if dir_path in seen_dirs:
            continue
        seen_dirs.add(dir_path)
        rows.append({"path": f"/{dir_path}", "is_dir": True})
    return rows


class ContextHubBackend(BackendProtocol):
    """Hub agent 仓库上的文件后端。直接基类只有协议，没有沙箱。"""

    def __init__(self, identifier: str, *, client: Client | None = None) -> None:
        self._identifier = identifier
        self._client = client if client is not None else Client()
        self._cache: dict[str, str] | None = None
        self._linked: dict[str, str] = {}
        self._commit: str | None = None
        self._mailbox = _Mailbox()
        self._worker: threading.Thread | None = None

    def _pull_snapshot(self) -> tuple[dict[str, str], dict[str, str], str | None]:
        """仓库不存在当成空快照。其它 ``LangSmithError`` 交给调用方。"""
        try:
            context = self._client.pull_agent(self._identifier)
        except LangSmithNotFoundError:
            return {}, {}, None

        files: dict[str, str] = {}
        linked: dict[str, str] = {}
        for stored_path, entry in context.files.items():
            if isinstance(entry, FileEntry):
                files[stored_path] = entry.content
                continue
            linked[stored_path] = entry.repo_handle
        return files, linked, context.commit_hash

    def _fill_cache_locked(self) -> dict[str, str]:
        if self._cache is None:
            files, linked, commit = self._pull_snapshot()
            self._cache = files
            self._linked = linked
            self._commit = commit
        if self._cache is None:
            raise RuntimeError(_CACHE_INIT_FAILED)
        return self._cache

    def _visible_locked(self) -> dict[str, str]:
        seen = dict(self._fill_cache_locked())
        for layer in (self._mailbox.in_flight, self._mailbox.pending):
            for queued in layer:
                _apply_overlay(seen, queued.changes)
        return seen

    def _visible(self) -> dict[str, str]:
        with self._mailbox.condition:
            return self._visible_locked()

    def _guarded_snapshot(self, log_template: str) -> tuple[dict[str, str] | None, str | None]:
        try:
            return self._visible(), None
        except LangSmithError as exc:
            logger.exception(log_template, self._identifier)
            return None, _hub_down(exc)

    def get_linked_entries(self) -> dict[str, str]:
        """链接路径到 ``repo_handle``。不叠加还没推送的文件变更。"""
        with self._mailbox.condition:
            self._fill_cache_locked()
            return dict(self._linked)

    def has_prior_commits(self) -> bool:
        """哈希不是 ``None`` 就是有过提交。空字符串也算有。"""
        with self._mailbox.condition:
            self._fill_cache_locked()
            return self._commit is not None

    def _enqueue_locked(
        self,
        changes: dict[str, str | None],
        *,
        intent: _WholeFile | _TextSwap | _TreeRemoval | None = None,
        occurrences: int | None = None,
    ) -> _Queued:
        copied = dict(changes)
        if intent is None:
            intent = _WholeFile(dict(copied))
        queued = _Queued(intent, copied, occurrences)
        box = self._mailbox
        if not box.pending:
            box.deadline = time.monotonic() + _BATCH_SECONDS
        box.pending.append(queued)
        if self._worker is None:
            try:
                worker = threading.Thread(target=self._drain, name=_THREAD_NAME, daemon=True)
                self._worker = worker
                worker.start()
            except BaseException:
                self._worker = None
                box.pending.remove(queued)
                if not box.pending:
                    box.deadline = None
                raise
        box.condition.notify_all()
        return queued

    @staticmethod
    def _await(queued: _Queued) -> None:
        queued.done.wait()
        if queued.error is not None:
            raise queued.error

    def _submit(self, changes: dict[str, str | None]) -> None:
        with self._mailbox.condition:
            self._fill_cache_locked()
            queued = self._enqueue_locked(changes)
        self._await(queued)

    def _take_batch(self) -> list[_Queued] | None:
        box = self._mailbox
        with box.condition:
            while box.pending:
                if box.deadline is None:
                    raise RuntimeError(_DEADLINE_MISSING)
                remaining = box.deadline - time.monotonic()
                if remaining > 0:
                    box.condition.wait(timeout=remaining)
                    continue
                batch = box.pending
                box.pending = []
                box.deadline = None
                box.in_flight = batch
                return batch
            self._worker = None
            return None

    def _replay_one(
        self, queued: _Queued, visible: dict[str, str], conflict: LangSmithConflictError
    ) -> tuple[dict[str, str | None], int | None]:
        intent = queued.intent
        if isinstance(intent, _WholeFile):
            return dict(intent.changes), None
        if isinstance(intent, _TextSwap):
            current = visible.get(intent.path)
            if current is None:
                raise conflict
            replaced = shared.perform_string_replacement(current, intent.old_string, intent.new_string, intent.replace_all)
            if isinstance(replaced, str):
                raise conflict
            body, count = replaced
            return {intent.path: body}, count
        base = intent.path
        prefix = base + "/"
        doomed = [key for key in visible if key == base or key.startswith(prefix)]
        return dict.fromkeys(doomed, None), None

    def _replay_all(self, rows: list[_Queued], visible_seed: dict[str, str], conflict: LangSmithConflictError) -> None:
        """全部重放成功才写回。中途失败时，前面几笔的 ``changes`` 保持原样。"""
        staged: list[tuple[_Queued, dict[str, str | None], int | None]] = []
        rolling = dict(visible_seed)
        for queued in rows:
            changes, count = self._replay_one(queued, rolling, conflict)
            staged.append((queued, changes, count))
            _apply_overlay(rolling, changes)
        for queued, changes, count in staged:
            queued.changes = changes
            queued.occurrences = count

    def _reload(self, conflict: LangSmithConflictError) -> None:
        files, linked, commit = self._pull_snapshot()
        dropped: list[_Queued] = []
        box = self._mailbox
        with box.condition:
            self._replay_all(box.in_flight, files, conflict)
            staged_base = dict(files)
            for queued in box.in_flight:
                _apply_overlay(staged_base, queued.changes)
            try:
                self._replay_all(box.pending, staged_base, conflict)
            except LangSmithConflictError as exc:
                dropped = box.pending
                box.pending = []
                box.deadline = None
                for queued in dropped:
                    queued.error = exc
            self._cache = files
            self._linked = linked
            self._commit = commit
        for queued in dropped:
            queued.done.set()

    def _hash_from_url(self, url: str) -> str | None:
        try:
            path = urlsplit(url).path
            owner, name, _hash = parse_hub_identifier(self._identifier)
        except ValueError:
            return None
        context_prefix = f"/context/{name}/"
        hub_prefix = f"/hub/{owner}/{name}:"
        for prefix in (context_prefix, hub_prefix):
            if not path.startswith(prefix):
                continue
            token = path.removeprefix(prefix)
            if _HASH_PATTERN.fullmatch(token):
                return token
        return None

    def _send_batch(self, batch: list[_Queued]) -> tuple[dict[str, str | None], str | None, tuple | None]:
        attempt = 0
        while attempt <= _CONFLICT_RETRIES:
            with self._mailbox.condition:
                merged = _merge(batch)
                parent = self._commit
            if not merged:
                return merged, parent, None
            try:
                url = self._client.push_agent(self._identifier, files=_to_payload(merged), parent_commit=parent)
            except LangSmithConflictError as conflict:
                if attempt == _CONFLICT_RETRIES:
                    raise
                self._reload(conflict)
                attempt += 1
                continue
            parsed = self._hash_from_url(url)
            if parsed is not None:
                return merged, parsed, None
            snapshot = self._pull_snapshot()
            resolved = snapshot[2]
            if resolved is None:
                raise RuntimeError(_HASH_UNRESOLVED)
            return merged, resolved, snapshot
        raise RuntimeError(_RETRY_EXHAUSTED)

    def _finish_batch(
        self,
        batch: list[_Queued],
        changes: dict[str, str | None],
        commit_hash: str | None,
        *,
        snapshot: tuple | None,
    ) -> bool:
        dropped: list[_Queued] = []
        box = self._mailbox
        with box.condition:
            if snapshot is None:
                base = dict(self._fill_cache_locked())
                _apply_overlay(base, changes)
                self._cache = base
            else:
                files, linked, _ignored = snapshot
                if box.pending:
                    fresh_conflict = LangSmithConflictError(_PENDING_STALE)
                    try:
                        self._replay_all(box.pending, files, fresh_conflict)
                    except LangSmithConflictError as exc:
                        dropped = box.pending
                        box.pending = []
                        box.deadline = None
                        for queued in dropped:
                            queued.error = exc
                self._cache = files
                self._linked = linked
            self._commit = commit_hash
            box.in_flight = []
            drained = not box.pending
            if drained:
                self._worker = None
        for queued in batch:
            queued.done.set()
        for queued in dropped:
            queued.done.set()
        return drained

    def _abort(self, batch: list[_Queued], error: BaseException) -> None:
        box = self._mailbox
        with box.condition:
            affected = [*batch, *box.pending]
            box.in_flight = []
            box.pending = []
            box.deadline = None
            self._cache = None
            self._linked = {}
            self._commit = None
            self._worker = None
            for queued in affected:
                queued.error = error
        for queued in affected:
            queued.done.set()

    def _drain(self) -> None:
        while True:
            batch: list[_Queued] = []
            try:
                nxt = self._take_batch()
                if nxt is None:
                    return
                batch = nxt
                changes, commit_hash, snapshot = self._send_batch(batch)
                drained = self._finish_batch(batch, changes, commit_hash, snapshot=snapshot)
            except BaseException as exc:
                self._abort(batch, exc)
                return
            if drained:
                return

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        hub_path = _strip_slashes(file_path)
        snapshot, failure = self._guarded_snapshot("Hub pull failed for %r")
        if failure is not None:
            return ReadResult(error=failure)
        assert snapshot is not None
        body = snapshot.get(hub_path)
        if body is None:
            return ReadResult(error=f"File '{file_path}' not found")
        stored = shared.create_file_data(body)
        return shared.slice_read_response(stored, offset, limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        hub_path = _strip_slashes(file_path)
        try:
            self._submit({hub_path: content})
        except LangSmithError as exc:
            logger.exception("Hub write failed for %r", self._identifier)
            return WriteResult(error=_hub_down(exc))
        return WriteResult(path=file_path)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        hub_path = _strip_slashes(file_path)
        try:
            with self._mailbox.condition:
                current = self._visible_locked().get(hub_path)
                if current is None:
                    return EditResult(error=f"Error: File '{file_path}' not found")
                replaced = shared.perform_string_replacement(current, old_string, new_string, replace_all)
                if isinstance(replaced, str):
                    return EditResult(error=replaced)
                new_body, count = replaced
                queued = self._enqueue_locked(
                    {hub_path: new_body},
                    intent=_TextSwap(hub_path, old_string, new_string, replace_all),
                    occurrences=count,
                )
            self._await(queued)
        except LangSmithError as exc:
            logger.exception("Hub edit failed for %r", self._identifier)
            return EditResult(error=_hub_down(exc))
        return EditResult(path=file_path, occurrences=queued.occurrences)

    def delete(self, file_path: str) -> DeleteResult:
        hub_path = _strip_slashes(file_path)
        try:
            with self._mailbox.condition:
                snapshot = self._visible_locked()
                base = hub_path.rstrip("/")
                prefix = base + "/"
                doomed = [key for key in snapshot if key == base or key.startswith(prefix)]
                if not doomed:
                    return DeleteResult(error=f"Error: File '{file_path}' not found")
                queued = self._enqueue_locked(dict.fromkeys(doomed, None), intent=_TreeRemoval(base))
            self._await(queued)
        except LangSmithError as exc:
            logger.exception("Hub delete failed for %r", self._identifier)
            return DeleteResult(error=_hub_down(exc))
        return DeleteResult(path=file_path)

    def ls(self, path: str = "/") -> LsResult:
        hub_prefix = _strip_slashes(path).rstrip("/")
        snapshot, failure = self._guarded_snapshot("Hub pull failed for %r")
        if failure is not None:
            return LsResult(error=failure)
        assert snapshot is not None
        return LsResult(entries=_list_children(snapshot, hub_prefix))

    def grep(
        self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None
    ) -> GrepResult:
        snapshot, failure = self._guarded_snapshot("Hub pull failed for %r")
        if failure is not None:
            return GrepResult(error=failure)
        assert snapshot is not None
        prefix = _grep_prefix(path)
        matcher = _fnmatch_filter(glob)
        hits: list = []
        for stored_path, body in snapshot.items():
            if prefix and not stored_path.startswith(prefix):
                continue
            if matcher is not None and matcher.match(os.path.normcase(stored_path)) is None:
                continue
            if _keep_adding(hits, stored_path, body, pattern, max_count):
                return GrepResult(matches=hits, truncated=True)
        return GrepResult(matches=hits)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """``path`` 是协议参数。这个后端的命名空间是平的，匹配时不看它。"""
        snapshot, failure = self._guarded_snapshot("Hub pull failed for %r")
        if failure is not None:
            return GlobResult(error=failure)
        assert snapshot is not None
        try:
            matcher = shared.compile_grep_include_glob(pattern)
        except shared.InvalidGlobPatternError as exc:
            logger.warning("Refused glob pattern %r for %r: %s", pattern, self._identifier, exc)
            return GlobResult(error=str(exc))
        rows = [{"path": f"/{stored_path}", "is_dir": False} for stored_path in snapshot if matcher(stored_path)]
        return GlobResult(matches=rows)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        decoded: list[tuple[str, str | None]] = []
        valid: dict[str, str] = {}
        for path, raw in files:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                decoded.append((path, None))
                continue
            decoded.append((path, text))
            valid[_strip_slashes(path)] = text
        commit_error: str | None = None
        if valid:
            try:
                self._submit(valid)
            except LangSmithError as exc:
                logger.exception("Hub batch upload failed for %r", self._identifier)
                commit_error = _hub_down(exc)
        results: list[FileUploadResponse] = []
        for path, text in decoded:
            if text is None:
                results.append(FileUploadResponse(path=path, error=INVALID_PATH))
                continue
            if commit_error is not None:
                results.append(FileUploadResponse(path=path, error=commit_error))
                continue
            results.append(FileUploadResponse(path=path))
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        snapshot, failure = self._guarded_snapshot("Hub pull failed for %r")
        if failure is not None:
            return [FileDownloadResponse(path=item, error=failure) for item in paths]
        assert snapshot is not None
        rows: list[FileDownloadResponse] = []
        for item in paths:
            body = snapshot.get(_strip_slashes(item))
            if body is not None:
                rows.append(FileDownloadResponse(path=item, content=body.encode("utf-8")))
                continue
            rows.append(FileDownloadResponse(path=item, error=FILE_NOT_FOUND))
        return rows
