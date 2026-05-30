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
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

Op = Literal["replace_first", "replace_all", "insert_before", "insert_after"]


def edit(
    path: str,
    op: Op,
    anchor: str,
    text: str,
    purpose: str,
) -> dict[str, Any]:
    """
    Edit an existing file using a single anchor-based operation.

    Use this tool when:
      - You are modifying an existing file.
      - The change can be described relative to a literal anchor string.
      - The modification is small or localized.

    Args:
      path: Existing file path.
      op: One of:
          - replace_first
          - replace_all
          - insert_before
          - insert_after
      anchor: Literal substring to match.
      text: Replacement or inserted content.
      purpose: Why this file is being modified.

    Returns:
      {
        "status": "ok" | "error",
        "message": str,
        "changed": bool
      }
    """
    p = Path(path)
    normalized_purpose = str(purpose or "").strip()
    if not normalized_purpose:
        return {"status": "error", "message": "purpose is required", "changed": False}
    if not p.exists():
        return {"status": "error", "message": "File not found", "changed": False}
    if not anchor:
        return {"status": "error", "message": "Anchor cannot be empty", "changed": False}

    original = p.read_text(encoding="utf-8")

    if op == "replace_all":
        if anchor not in original:
            return {"status": "error", "message": "Anchor not found", "changed": False}
        new_text = original.replace(anchor, text)
    else:
        idx = original.find(anchor)
        if idx == -1:
            return {"status": "error", "message": "Anchor not found", "changed": False}
        if op == "replace_first":
            new_text = original[:idx] + text + original[idx + len(anchor) :]
        elif op == "insert_before":
            new_text = original[:idx] + text + original[idx:]
        elif op == "insert_after":
            new_text = original[: idx + len(anchor)] + text + original[idx + len(anchor) :]
        else:
            return {"status": "error", "message": "Invalid op", "changed": False}

    changed = new_text != original
    if changed:
        p.write_text(new_text, encoding="utf-8")

    return {
        "status": "ok",
        "message": "File updated" if changed else "No changes",
        "changed": changed,
    }
