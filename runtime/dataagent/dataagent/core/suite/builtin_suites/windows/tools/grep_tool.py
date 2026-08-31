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
"""Windows Suite local tool: pure-Python grep replacement for Windows."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.actions.tools.local_tool.tools import _resolve_and_authorize
from dataagent.utils.constants import DEFAULT_GREP_HEAD_LIMIT, DEFAULT_SKIP_DIRS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GREP_NO_MATCHES = "(no matches)"
_GREP_TRUNCATION_SUFFIX = "\n(Results truncated. Consider a more specific pattern or glob filter.)"

# file_type → extensions, same mapping as the system grep tool
_GREP_FILE_TYPE_MAP: dict[str, tuple[str, ...]] = {
    "py": (".py", ".pyi"),
    "js": (".js", ".mjs", ".cjs", ".jsx"),
    "ts": (".ts", ".tsx"),
    "go": (".go",),
    "rust": (".rs",),
    "java": (".java",),
    "c": (".c", ".h"),
    "cpp": (".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".h"),
    "md": (".md", ".markdown"),
    "json": (".json",),
    "yaml": (".yaml", ".yml"),
    "toml": (".toml",),
    "html": (".html", ".htm"),
    "css": (".css",),
    "sh": (".sh", ".bash", ".zsh"),
    "sql": (".sql",),
    "xml": (".xml",),
    "txt": (".txt",),
}

# Max bytes to read from a single file to avoid OOM on huge files
_MAX_FILE_READ_BYTES = 4 * 1024 * 1024  # 4 MiB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _should_skip_dir(dir_name: str) -> bool:
    """Return True if directory should be skipped during traversal."""
    return dir_name in DEFAULT_SKIP_DIRS or dir_name.startswith(".")


def _should_include_file(
    path: Path,
    *,
    glob_pattern: str | None,
    file_type: str | None,
) -> bool:
    """Check if a file matches the glob / file_type filters."""
    if not path.is_file():
        return False

    # file_type filter (maps to extensions)
    if file_type:
        exts = _GREP_FILE_TYPE_MAP.get(file_type.lower())
        if exts and path.suffix.lower() not in exts:
            return False

    # glob_pattern filter
    if glob_pattern:
        # Simple glob: match against filename
        from fnmatch import fnmatch

        if not fnmatch(path.name, glob_pattern):
            # Also try matching the full relative pattern like **/*.py
            if "*" not in glob_pattern and "?" not in glob_pattern:
                return False
            if not fnmatch(path.name, glob_pattern.lstrip("*").lstrip("/")):
                return False

    return True


def _is_binary_file(data: bytes) -> bool:
    """Heuristic: if the first 8 KiB contain a NUL byte, treat as binary."""
    return b"\x00" in data[:8192]


def _grep_file(
    path: Path,
    pattern: re.Pattern[str],
    *,
    output_mode: str,
    before: int,
    after: int,
    context: int = 0,
) -> list[str]:
    """Search a single file.

    Always returns a list of strings so the caller can uniformly check
    ``if result``.  The content varies by *output_mode*:

    - ``"files_with_matches"`` — ``["<path>"]`` on first match, ``[]`` otherwise.
    - ``"count"`` — ``["<path>:<count>"]`` if count > 0, ``[]`` otherwise.
    - ``"content"`` — ``["<line_no>\\t<line>", ...]`` for each match with context,
      ``[]`` otherwise.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return []

    if _is_binary_file(raw):
        return []

    # Truncate huge files
    if len(raw) > _MAX_FILE_READ_BYTES:
        raw = raw[:_MAX_FILE_READ_BYTES]

    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return []

    lines = text.splitlines()
    if not lines:
        return []

    if output_mode == "files_with_matches":
        for line in lines:
            if pattern.search(line):
                return [str(path)]
        return []

    if output_mode == "count":
        count = sum(1 for line in lines if pattern.search(line))
        return [f"{path}:{count}"] if count > 0 else []

    # output_mode == "content" — context overrides before/after when set
    effective_before = context if context > 0 else before
    effective_after = context if context > 0 else after

    result_lines: list[str] = []
    matches: list[int] = []
    for i, line in enumerate(lines):
        if pattern.search(line):
            matches.append(i)

    if not matches:
        return []

    seen_ranges: set[int] = set()
    for match_idx in matches:
        start = max(0, match_idx - effective_before)
        end = min(len(lines) - 1, match_idx + effective_after)
        for i in range(start, end + 1):
            if i not in seen_ranges:
                seen_ranges.add(i)
                result_lines.append(f"{i + 1}\t{lines[i]}")

    return result_lines


# ---------------------------------------------------------------------------
# Public tool
# ---------------------------------------------------------------------------


def win_grep(
    pattern: str,
    path: str | None = None,
    glob_pattern: str | None = None,
    output_mode: str = "files_with_matches",
    head_limit: int = DEFAULT_GREP_HEAD_LIMIT,
    before: int = 0,
    after: int = 0,
    context: int = 0,
    case_insensitive: bool = False,
    file_type: str | None = None,
) -> dict[str, Any]:
    """Pure-Python content search tool for Windows (replaces system ``grep``).

    ALWAYS use this tool for content search tasks on Windows. NEVER invoke
    ``grep`` as a cmd command directly — this tool handles file traversal,
    binary detection, and encoding correctly.

    Usage:
    - Supports Python ``re`` regex (POSIX ERE compatible)
    - Filter files with glob_pattern (e.g. ``"*.js"``, ``"*.tsx"``) or
      file_type (e.g. ``"py"``, ``"js"``, ``"rust"``)
    - Output modes: ``"files_with_matches"`` shows only file paths (default),
      ``"content"`` shows matching lines, ``"count"`` shows match counts
    - Results are capped by head_limit (default 250). Pass 0 for unlimited
      (use sparingly — large result sets waste context).

    Args:
        pattern (str): The regular expression pattern to search for in file contents.
        path (str | None): File or directory to search in. Defaults to workspace root.
        glob_pattern (str | None): Glob pattern to filter files (e.g. ``"*.py"``).
        output_mode (str): ``"files_with_matches"`` (default), ``"content"``, or ``"count"``.
        head_limit (int): Limit output to first N entries (default 250).
        before (int): Lines of context before each match. Requires ``output_mode="content"``.
        after (int): Lines of context after each match. Requires ``output_mode="content"``.
        context (int): Lines of context before and after each match. Overrides before/after.
        case_insensitive (bool): Case-insensitive search. Default false.
        file_type (str | None): File type filter (e.g. ``"py"``, ``"js"``).
    """
    if not pattern:
        raise ValueError("'pattern' is required and must not be empty.")

    search_root = path or str(get_current_sandbox().workspace_root)
    root_path = _resolve_and_authorize(search_root, "path", operation="grep", mode="read")
    if not root_path.exists():
        raise FileNotFoundError(f"Path not found: {search_root}")

    # Compile regex
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern: {pattern!r} — {exc}") from exc

    # Collect files
    if root_path.is_file():
        files_to_search = [root_path]
    else:
        files_to_search: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root_path):
            # Skip hidden/VCS directories in-place
            dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                if _should_include_file(fpath, glob_pattern=glob_pattern, file_type=file_type):
                    files_to_search.append(fpath)

    # Search
    resolved_root = str(root_path.resolve())
    output_lines: list[str] = []
    truncated = False
    file_count = 0

    for fpath in files_to_search:
        if output_mode == "files_with_matches":
            result = _grep_file(fpath, compiled, output_mode="files_with_matches", before=0, after=0)
            if result:
                rel = os.path.relpath(str(fpath.resolve()), resolved_root)
                output_lines.append(rel)
                file_count += 1
                if head_limit > 0 and file_count >= head_limit:
                    truncated = True
                    break

        elif output_mode == "count":
            result = _grep_file(fpath, compiled, output_mode="count", before=0, after=0)
            if result:
                # result is ["<path>:<count>"]; use the relative path instead
                rel = os.path.relpath(str(fpath.resolve()), resolved_root)
                count = result[0].rsplit(":", 1)[-1]
                output_lines.append(f"{rel}:{count}")
                file_count += 1
                if head_limit > 0 and file_count >= head_limit:
                    truncated = True
                    break

        else:  # content
            result = _grep_file(
                fpath,
                compiled,
                output_mode="content",
                before=before,
                after=after,
                context=context,
            )
            if result:
                # Prepend file path for each match line
                rel = os.path.relpath(str(fpath.resolve()), resolved_root)
                for line in result:
                    output_lines.append(f"{rel}:{line}")
                file_count += 1
                if head_limit > 0 and len(output_lines) >= head_limit:
                    output_lines = output_lines[:head_limit]
                    truncated = True
                    break

    output = "\n".join(output_lines) if output_lines else _GREP_NO_MATCHES
    if truncated:
        output += _GREP_TRUNCATION_SUFFIX

    resolved = root_path.resolve()
    return {
        "original_msg": output,
        "frontend_msg": f"\n\ngrep 工具执行完成\n\n在 {resolved} 中搜索完成",
        "data": {
            "pattern": pattern,
            "path": str(resolved),
            "glob": glob_pattern,
            "output_mode": output_mode,
            "head_limit": head_limit,
            "truncated": truncated,
        },
    }
