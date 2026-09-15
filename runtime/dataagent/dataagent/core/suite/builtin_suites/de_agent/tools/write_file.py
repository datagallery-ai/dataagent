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
import fnmatch
from pathlib import Path
from typing import Any

from dataagent.actions.tools.local_tool.tools import write_file as _write_file


def write_file(path: str, content: str, purpose: str) -> dict[str, Any]:
    """Write a file to the local filesystem.

    Writes a file to the workspace. If the file already exists it will be
    overwritten; parent directories are created automatically.

    Usage:
    - Use absolute paths under the workspace root. Never write into skill
      directories or read-only roots.
    - This tool will overwrite the existing file if there is one at the
      provided path.
    - If this is an existing file, you MUST use the read_file tool first to
      read the file's contents. This tool will fail if you did not read the
      file first.
    - Prefer the edit_file tool for modifying existing files — it only sends
      the diff. Only use this tool to create new files or for complete rewrites.
    - NEVER create documentation files (*.md) or README files unless explicitly
      requested by the user.
    - Keep ``content`` concise — ideally under 300 lines. For larger files,
      write a short skeleton first, then use edit_file to add sections
      incrementally. This avoids slow, token-heavy tool calls.

    Args:
        path (str): Absolute path under the workspace root for the file to create or overwrite.
        content (str): The content to write to the file.
        purpose (str): Brief description of why this file is being created/updated (required, non-empty).
    """
    if _is_insert_sql_filename(path) and not Path(path).is_file():
        return {
            "original_msg": "insert_*.sql files must be created by the nl2sql subagent first "
            "and then can be modified by write_file. "
            "Cannot write these files directly. "
            "Use bash to copy files if there is already an insert_*.sql file in the workspace.",
            "frontend_msg": "insert_*.sql files must be created by the nl2sql subagent first "
            "and then can be modified by write_file. "
            "Cannot write these files directly. "
            "Use bash to copy files if there is already an insert_*.sql file in the workspace.",
        }

    return _write_file(path, content, purpose)


def _is_insert_sql_filename(path: str, pattern: str = "insert_*.sql") -> bool:
    """path 的最后一段文件名是否匹配 insert_*.sql 这类模式。"""
    name = Path(str(path or "").strip()).name
    if not name:
        return False

    return fnmatch.fnmatchcase(name, pattern)
