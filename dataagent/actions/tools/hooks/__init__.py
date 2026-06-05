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
"""Per-tool-call pre/post hooks (Flex Executor)."""

from dataagent.actions.tools.hooks.base import (
    ToolHookInvocation,
    ToolHookRunner,
    ToolPostHookOutcome,
    ToolPreHookOutcome,
    readonly_tool_args,
)
from dataagent.actions.tools.hooks.config import ToolHookLists, load_tool_hooks_from_config

# Example hooks live under ``dataagent.actions.tools.hooks.examples`` (see example.yaml).

__all__ = [
    "ToolHookInvocation",
    "ToolHookRunner",
    "ToolHookLists",
    "ToolPreHookOutcome",
    "ToolPostHookOutcome",
    "load_tool_hooks_from_config",
    "readonly_tool_args",
]
