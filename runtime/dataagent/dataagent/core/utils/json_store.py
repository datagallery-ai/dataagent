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
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any


def read_json_object(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Read JSON object."""
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return payload if isinstance(payload, dict) else default


def write_json_object(path: Path, payload: dict[str, Any], *, atomic: bool = True) -> None:
    """Write JSON object."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    if not atomic:
        path.write_text(content, encoding="utf-8")
        return
    # Concurrent cancellation/completion can publish the same job result. Each
    # writer needs its own sibling file until its atomic replacement succeeds.
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as temporary:
            tmp_path = Path(temporary.name)
            temporary.write(content)
        tmp_path.replace(path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
