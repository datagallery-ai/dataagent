# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""只读访问 DataTaskIR 的**持久化**口径快照。

``ir_field_values`` 是 ``Runtime`` 的进程内缓存，只在触发 IR post-hook 的那一轮
有值；「第二轮对话执行数据校验」不触发 hook → 缓存为空 → 主键拿不到 →
``pk_duplicate_rate`` 与 ``source_reconciliation`` 整项跳过。

而权威口径本来就持久化在 IR 会话快照的 ``fields`` 里（*"Runtime caches are
never the source of truth."*）。本模块提供三级回落（越靠前越新）：

1. ``ir_field_values`` 缓存 —— 本轮刚填好的口径；
2. ``ir_session.snapshot`` —— 本轮开头 ``prepare_turn`` 从磁盘恢复的快照；
3. 磁盘上最近一次 committed 的快照。

只认 ``committed``；草稿 / 待澄清关系的快照不是权威口径。绝不抛异常、绝不写盘。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dataagent.actions.tools.hooks.examples.data_task_ir_spike.session_store import IRSessionStore
from dataagent.utils.log import logger
from dataagent.utils.runtime_paths import resolve_flex_session_memory_dir

__all__ = ["load_committed_fields"]


def load_committed_fields(runtime: Any) -> list[dict[str, Any]]:
    """读取当前会话**已冻结**（committed）的 IR 字段口径。

    Returns:
        ``[{field_id, value}, ...]``；读不到时返回空列表（**不猜、不抛异常**）。
    """
    for loader in (_cache_fields, _session_fields, _disk_fields):
        try:
            fields = loader(runtime)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[DataTaskIR reader] {loader.__name__} failed: {exc!r}")
            continue
        if fields:
            return fields
    return []


def _cache_fields(runtime: Any) -> list[dict[str, Any]]:
    """``ir_field_values`` 缓存 —— 本轮刚填好的口径（最新）。"""
    if runtime is None:
        return []
    try:
        raw = runtime.get_cache("ir_field_values")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[DataTaskIR reader] ir_field_values unreadable: {exc!r}")
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _session_fields(runtime: Any) -> list[dict[str, Any]]:
    """``ir_session.snapshot`` —— 本轮开头 ``prepare_turn`` 从磁盘恢复的快照。"""
    if runtime is None:
        return []
    try:
        session = runtime.get_cache("ir_session")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[DataTaskIR reader] ir_session unreadable: {exc!r}")
        return []
    if not isinstance(session, dict):
        return []
    return _committed_fields(session.get("snapshot"))


def _disk_fields(runtime: Any) -> list[dict[str, Any]]:
    """磁盘上的最近一次 committed 快照（``current.json`` → ``versions/``）。"""
    store = _open_store(runtime)
    if store is None:
        return []
    try:
        current = store.load()
    except (ValueError, OSError) as exc:
        logger.debug(f"[DataTaskIR reader] current.json unusable: {exc!r}")
        current = None
    fields = _committed_fields(current)
    if fields:
        return fields
    return _latest_committed_version(store.root / "versions")


def _open_store(runtime: Any) -> IRSessionStore | None:
    """按当前会话身份定位 IR 快照存储（与 ``prepare_turn`` 同一套路径解析）。

    只对 MAIN 层级的主 Agent 生效 —— 快照按 user/session 定位，子 Agent 与主
    Agent 共享同一份存储，而子 Agent 不应继承主 Agent 的历史口径。
    """
    workspace = getattr(runtime, "workspace_dir", None)
    user_id = str(getattr(runtime, "user_id", "") or "").strip()
    session_id = str(getattr(runtime, "session_id", "") or "").strip()
    if not workspace or not user_id or not session_id:
        return None
    if int(getattr(runtime, "sub_id", 0) or 0) != 0:
        return None
    if str(getattr(runtime, "hierarchy", "MAIN") or "MAIN").upper() != "MAIN":
        return None
    config = None
    getter = getattr(runtime, "get_all_config", None)
    if callable(getter):
        try:
            config = getter()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[DataTaskIR reader] get_all_config failed: {exc!r}")
            config = None
    memory = resolve_flex_session_memory_dir(
        workspace=workspace,
        user_id=user_id,
        session_id=session_id,
        config=config,
    )
    return IRSessionStore(memory, user_id, session_id)


def _latest_committed_version(versions_dir: Path) -> list[dict[str, Any]]:
    """``versions/`` 里最近一次 committed 的快照字段。

    ``current.json`` 是可变指针 —— 任务关系变更会把它改写成 draft，那时
    ``current.json`` 里没有口径了，但 ``versions/`` 里的历史版本不可变。
    """
    try:
        if not versions_dir.is_dir():
            return []
        paths = sorted(versions_dir.glob("*.json"))
    except OSError as exc:
        logger.debug(f"[DataTaskIR reader] versions dir unusable: {exc!r}")
        return []
    best: list[dict[str, Any]] = []
    best_key = ""
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug(f"[DataTaskIR reader] version file unreadable: {path.name} ({exc!r})")
            continue
        fields = _committed_fields(value)
        if not fields:
            continue
        key = str(value.get("updated_at") or "")
        if not key:
            try:
                key = f"{path.stat().st_mtime:.6f}"
            except OSError:
                key = ""
        if best_key == "" or key > best_key:
            best, best_key = fields, key
    return best


def _committed_fields(snapshot: Any) -> list[dict[str, Any]]:
    """从快照里取**已冻结**的字段（``status == "committed"``）。

    草稿与待澄清关系的快照不是权威口径，拿它当主键声明就是在猜。
    """
    if not isinstance(snapshot, dict) or snapshot.get("status") != "committed":
        return []
    fields = snapshot.get("fields")
    if not isinstance(fields, list):
        return []
    return [item for item in fields if isinstance(item, dict)]
