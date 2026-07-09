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
"""Shared async subprocess helpers for core job and resource runners."""

from __future__ import annotations

import asyncio
import os
import signal


async def terminate_process_tree_async(process: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess and its process group when still running.

    Args:
        process: Async subprocess handle created with ``start_new_session`` on Unix.
    """
    if process.returncode is None:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
