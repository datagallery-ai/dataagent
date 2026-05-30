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
"""BaseHook Protocol for galatea-style hook chains.

A hook is a ``(state, runtime) -> state`` callable that can be attached to
nodes or agents as pre/post-processing steps.  Multiple hooks are chainable:
each receives the output of the previous one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from dataagent.core.cbb.base_state import BaseState
    from dataagent.core.cbb.runtime import Runtime


class BaseHook(Protocol):
    def __call__(
        self,
        state: BaseState,
        runtime: Runtime | None = None,
    ) -> BaseState:
        """Call the hook."""
        pass
