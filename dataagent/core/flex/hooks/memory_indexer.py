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
"""Memory indexer: 将 session 记忆追加到 MEMORY.md。

在 portraiter 之后调用，将当前 session 的 snapshot 信息增量写入 MEMORY.md。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loguru import logger

from dataagent.utils.runtime_paths import resolve_layout_dir, resolve_session_framework_workspace, resolve_user_root


def _get_memory_md_path(user_id: str) -> Path:
    """返回 MEMORY.md 路径。"""
    return resolve_user_root(user_id=user_id) / ".memory" / "MEMORY.md"


def _load_snapshot(
    user_id: str,
    session_id: str,
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """加载当前 session 的 snapshot。"""
    root = resolve_session_framework_workspace(
        workspace=workspace,
        config=config,
        session_id=session_id,
        user_id=user_id,
    )
    snap_path = resolve_layout_dir(root, "session_memory_dir", config=config) / "snapshot.json"
    if not snap_path.exists():
        return {}
    try:
        data = json.loads(snap_path.read_text(encoding="utf-8"))
        # 兼容旧格式（外层有 user_snapshot）和新格式（直接是 snapshot 内容）
        return data.get("user_snapshot") if "user_snapshot" in data else data
    except (OSError, json.JSONDecodeError):
        return {}


def _load_profile(user_id: str) -> dict[str, Any]:
    """加载用户 profile。"""
    profile_path = resolve_user_root(user_id=user_id) / ".memory" / "profile.json"
    if not profile_path.exists():
        return {}
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
        return data.get("user_profile", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _format_session_entry(session_id: str, snapshot: dict[str, Any]) -> str:
    """将一个 session 的 snapshot 格式化为 MEMORY.md 的一个章节。"""
    summary = snapshot.get("session_summary", "No summary")

    lines = [
        f"### {session_id}",
        f"- **Summary**: {summary}",
    ]

    return "\n".join(lines)


def _update_memory_md(user_id: str, session_id: str, snapshot: dict[str, Any]) -> None:
    """增量更新 MEMORY.md：追加当前 session 的章节。"""
    memory_md_path = _get_memory_md_path(user_id)
    memory_md_path.parent.mkdir(parents=True, exist_ok=True)

    # 读取现有内容
    existing_content = ""
    if memory_md_path.exists():
        try:
            existing_content = memory_md_path.read_text(encoding="utf-8")
        except OSError:
            existing_content = ""

    # 检查是否已存在该 session 的章节
    if re.search(rf"^###\s+{re.escape(session_id)}$", existing_content, re.MULTILINE):
        # 已存在，更新该章节
        logger.debug(f"[memory_indexer] session {session_id} already indexed, skipping")
        return

    # 构建新章节
    new_entry = _format_session_entry(session_id, snapshot)

    # 追加到 Session Summaries 部分之后
    if "# Session Summaries" in existing_content:
        # 找到第一个 ### 章节之前插入
        pattern = r"(# Session Summaries\n\n)"
        replacement = r"\1" + new_entry + "\n\n"
        new_content = re.sub(pattern, replacement, existing_content, count=1)
    else:
        # 没有 Session Summaries 部分，在 User Profile 之后添加
        if "# User Profile" in existing_content:
            pattern = r"(# User Profile.*?(?=\n##|\n#|$))"
            new_content = existing_content + "\n\n## Session Summaries\n\n" + new_entry + "\n"
        else:
            # 全新文件
            new_content = f"# Memory for {user_id}\n\n## Session Summaries\n\n{new_entry}\n"

    # 原子写入
    tmp = memory_md_path.with_suffix(".md.tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(memory_md_path)
    logger.debug(f"[memory_indexer] indexed session {session_id}")


def update_memory_index(
    user_id: str,
    session_id: str,
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> None:
    """入口函数：在 session 结束后调用，更新 MEMORY.md。"""
    if not user_id or not session_id:
        return

    try:
        snapshot = _load_snapshot(user_id, session_id, workspace=workspace, config=config)
        if not snapshot or not snapshot.get("session_summary"):
            logger.debug(f"[memory_indexer] no valid snapshot for session {session_id}")
            return
        _update_memory_md(user_id, session_id, snapshot)
    except Exception as e:
        logger.debug(f"[memory_indexer] failed to update MEMORY.md: {e}")
