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
"""Shared repaired-JSON utilities for DataTaskIR model calls."""

# ruff: noqa: UP045 -- repository convention requires Optional for nullable annotations.

from __future__ import annotations

from typing import Any, Optional

import json_repair


def repair_json_object(raw_text: str) -> Optional[dict[str, Any]]:
    """Repair an LLM response as JSON and return it when the repaired value is an object."""
    try:
        parsed = json_repair.loads(raw_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


async def ainvoke_json_object(llm: Any, messages: list[dict[str, str]]) -> tuple[Optional[dict[str, Any]], str]:
    """Call an LLM for a JSON object and repair its raw response before returning it."""
    response = await llm.ainvoke(messages, response_format={"type": "json_object"})
    raw_text = str(response.content or "")
    return repair_json_object(raw_text), raw_text
