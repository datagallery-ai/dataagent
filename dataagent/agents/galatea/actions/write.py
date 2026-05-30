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
from pathlib import Path


def write(path: str, content: str, purpose: str) -> str:
    """
    Create or overwrite a text file in the workspace.

    Use this tool when:
    - Generating new files
    - Replacing entire file contents
    - Saving processed output

    Important:
    - Tool arguments must be valid JSON. Avoid malformed escaping in `content`.
    - Keep each write payload reasonably small; if content is large, write a short
      scaffold first, then add content with multiple `edit` calls.

    Args:
        path: Target file path.
        content: Full text content to write.
        purpose: Why this file is being created/updated.

    Returns:
        The path of the file that was written.
    """
    p = Path(path)
    normalized_purpose = str(purpose or "").strip()
    if not normalized_purpose:
        raise ValueError("purpose is required")

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content.encode("utf-8"))
    return str(p.resolve())
