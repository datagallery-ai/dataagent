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

DEFAULT_MAX_BYTES = 16384


def read(path: str, max_bytes: int | str | None = DEFAULT_MAX_BYTES) -> str:
    """
    Read a text file from the workspace.

    Use this tool when:
    - Inspecting source code
    - Reviewing Configuration files
    - Checking logs or outputs

    Do not use this tool when:
    - The target is a CSV or other data file and you have not inspected size/shape first.
      For data files, follow a safe preview-first workflow:
        - Check file size before full read.
        - Check rough length when useful.
        - Read only a small preview first (for example top 5 lines), then decide
        whether full content is needed.
    - You are considering `max_bytes="inf"` without first checking file size.
      This can push very large content into context and is strongly discouraged unless necessary.

    Args:
        path: Relative or absolute file path.
        max_bytes: Byte limit for reading file content. Defaults to 16384.
            Pass a smaller value for safer previews on large files.
            Use "inf" to read the full file.

    Returns:
        The content of the file, truncated with a clear suffix if the byte limit is reached.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File {path} not found")

    data = p.read_bytes()
    if isinstance(max_bytes, str):
        token = max_bytes.strip().lower()
        if token == "inf":
            return data.decode("utf-8", errors="ignore")
        raise ValueError("max_bytes must be an integer, None, or 'inf'")
    if max_bytes is None:
        max_bytes = DEFAULT_MAX_BYTES
    max_bytes = int(max_bytes)
    if max_bytes < 0:
        raise ValueError("max_bytes must be >= 0")

    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="ignore")

    clipped = data[:max_bytes].decode("utf-8", errors="ignore")
    return f"{clipped}\n...(truncated, read first {max_bytes} bytes out of {len(data)} bytes)"
