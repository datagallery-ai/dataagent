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
import subprocess
from typing import Any


def bash(command: str, purpose: str, timeout: int = 600) -> dict[str, Any]:
    """
    Execute a shell command in the current workspace.

    Use this tool when:
    - Running scripts or programs
    - Installing dependencies
    - Compiling or testing code
    - Calling CLI utilities (git, python, etc.)

    Args:
        command: Complete shell command as a single string.
        timeout (optional): Maximum time to wait for the command to complete, in seconds. Defaults to 600 seconds.
        purpose: Why this command is being run.

    Returns:
    {
        "exit_code": int,
        "stdout": str,
        "stderr": str,
    }
    """
    normalized_purpose = str(purpose or "").strip()
    if not normalized_purpose:
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": "purpose is required",
        }

    result = subprocess.run(
        command,
        shell=True,
        timeout=timeout,
        capture_output=True,
        text=True,
    )

    return {
        "exit_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr,
    }
