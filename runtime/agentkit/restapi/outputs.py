"""Read session output files for hosts, independently of Agent streaming events."""

import codecs
import os
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

PREVIEW_BYTES = 64 * 1024
_INTERNAL = {"artifacts/conversation_history", "artifacts/large_tool_results"}


def output_file(root: Path, relative: str) -> Path:
    """Only regular files below outputs; never follow links to other sessions."""
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith("/") or ".." in parts or "\x00" in relative:
        raise HTTPException(400, "Invalid output path")
    current = root
    for part in ("", *parts):
        current = current / part
        if current.is_symlink():
            raise HTTPException(404, "Output file not found")
    if any(PurePosixPath(relative).is_relative_to(path) for path in _INTERNAL):
        raise HTTPException(404, "Output file not found")
    if not current.is_file():
        raise HTTPException(404, "Output file not found")
    return current


def list_outputs(root: Path) -> list[dict]:
    if root.is_symlink():
        raise HTTPException(409, "Session outputs directory cannot be a symbolic link")
    if not root.exists():
        return []
    if not root.is_dir():
        raise HTTPException(409, "Session outputs path is not a directory")
    files = []

    def on_error(error):
        raise error  # Permission failures must not masquerade as an empty session.

    for directory, dirs, names in os.walk(root, onerror=on_error, followlinks=False):
        parent = Path(directory)
        dirs[:] = [name for name in dirs if not (parent / name).is_symlink()
                   and (parent / name).relative_to(root).as_posix() not in _INTERNAL]
        for name in names:
            path = parent / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue  # The Agent may still be writing/removing files.
            if not stat.S_ISREG(info.st_mode):
                continue
            files.append({
                "path": path.relative_to(root).as_posix(), "size": info.st_size,
                "modifiedAt": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
            })
    return sorted(files, key=lambda item: (item["modifiedAt"], item["path"]), reverse=True)


def preview_output(root: Path, relative: str) -> dict:
    path = output_file(root, relative)
    try:
        with path.open("rb") as stream:
            data = stream.read(PREVIEW_BYTES + 1)
    except FileNotFoundError:
        raise HTTPException(404, "Output file not found") from None
    truncated = len(data) > PREVIEW_BYTES
    try:
        if b"\x00" in data:
            raise ValueError("binary")
        content = codecs.getincrementaldecoder("utf-8")().decode(data[:PREVIEW_BYTES], final=not truncated)
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(415, "Preview supports UTF-8 text only; open the file locally") from None
    if truncated:
        content += "\n\n[Preview truncated at 64 KiB; open the file locally for full content.]"
    return {"type": "file", "path": f"/output/{relative}", "content": content}
