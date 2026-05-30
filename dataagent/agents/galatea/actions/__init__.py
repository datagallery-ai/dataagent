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
"""Galatea built-in tool functions.

Each module exposes a single callable whose name matches the module filename.
Tools are registered by passing the file path to ActionManager.register_tool().
"""

from pathlib import Path

_ACTIONS_DIR = Path(__file__).parent

_DEFAULT_TOOLS = ["bash", "read", "write", "edit", "inspect"]
_OPTIONAL_TOOLS = ["search_online", "create_subagent"]


def get_builtin_tool_paths(*, include_optional: bool = False) -> list[str]:
    """Return absolute file paths for galatea's built-in tools.

    Pass directly to ``Env(tools=...)`` for ActionManager registration.

    Args:
        include_optional: If True, also include ``search_online`` (requires
            a Tavily API key) and ``create_subagent`` (useful only when
            hierarchical orchestration is enabled).
    """
    names = _DEFAULT_TOOLS + (_OPTIONAL_TOOLS if include_optional else [])
    return [str(_ACTIONS_DIR / f"{name}.py") for name in names]
