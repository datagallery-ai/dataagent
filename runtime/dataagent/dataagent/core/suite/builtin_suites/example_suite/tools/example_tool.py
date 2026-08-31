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
"""Example Suite local tool: read a file from ``custom_dir`` via ``ConfigManager``."""

from __future__ import annotations

from dataagent.actions.tools.context import ToolExecutionContext


def read_suite_doc(
    filename: str = "guide.md",
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, str]:
    """
    Read a text file under ``<suite_root>/custom_dir/`` for an activated Suite.

    Args:
        filename: File name inside the Suite custom directory.
        _tool_context: Injected execution context with ``config_manager`` and tool ``config``.

    Returns:
        Dict with resolved ``path`` and file ``content``.
    """
    config_manager = _tool_context.config_manager
    if config_manager is None:
        raise RuntimeError("config_manager is not available")

    tool_cfg = _tool_context.tool_config or {}
    suite_name = str(tool_cfg.get("suite_name") or "example_suite").strip()
    subdir = str(tool_cfg.get("custom_subdir") or "custom_dir").strip()

    try:
        # 根据 suite_name 获取 suite 所在的绝对路径
        suite_root = config_manager.get_activated_suite_root(suite_name)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    doc_path = (suite_root / subdir / filename).resolve()
    doc_path.relative_to(suite_root)
    return {
        "path": str(doc_path),
        "content": doc_path.read_text(encoding="utf-8"),
    }
