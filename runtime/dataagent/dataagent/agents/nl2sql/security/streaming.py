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
"""Streaming boundary helpers for SQL awaiting security approval."""

from typing import Any

_PENDING_MESSAGE = "=== SQL Security ===\nSQL candidates are pending security validation."


def sanitize_stream_item(item: Any) -> Any:
    """Hide candidate SQL fields until the workflow marks one as security approved."""
    if isinstance(item, tuple) and len(item) == 2:
        mode, data = item
        return (mode, _sanitize_mapping(data))
    return _sanitize_mapping(item)


def _sanitize_mapping(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    result = {key: _sanitize_mapping(value) for key, value in data.items()}
    if result.get("security_sql_approved", False):
        return result
    if "generation_results" in result:
        result.update({"generation_results": []})
    if "validation_results" in result:
        result.update({"validation_results": []})
    if "sql" in result:
        result.update({"sql": ""})
    if "stream_message" in result and isinstance(result.get("stream_message", ""), str):
        result.update({"stream_message": _PENDING_MESSAGE})
    return result
