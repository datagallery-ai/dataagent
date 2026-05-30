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
"""DataAgent 核心运行时层。L1 可从本包导入 ``ReActAgent``（懒加载）。"""

from __future__ import annotations

__all__ = ["ReActAgent"]

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dataagent.core.interface import ReActAgent


def __getattr__(name: str) -> Any:
    if name == "ReActDataAgent":
        from dataagent.core.interface import ReActAgent

        return ReActAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
