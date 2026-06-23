# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""OpenJiuWen Workspace builder."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_workspace(root_path: str | Path, *, language: str = "cn") -> Any:
    """Create a single-root Jiuwen Workspace after validating the directory."""
    root = Path(root_path).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError(f"WORKSPACE.path must be a directory, got file: {root}")
    root.mkdir(parents=True, exist_ok=True)

    from openjiuwen.harness import Workspace

    return Workspace(root_path=root, language=language)
