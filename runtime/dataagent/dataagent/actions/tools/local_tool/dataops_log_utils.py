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
"""Shared bounding for DataOps error text that crosses the agent boundary.

DataOps runs Spark/Hive jobs. On failure the backend returns an inline
``errorLog`` that is routinely hundreds of KB: the full driver trace plus one
stack-trace block per failed task. Handing that to the agent verbatim floods
the context and degrades reasoning quality.

The same ``errorLog`` has two consumers with opposite needs:

* ``_analyze_log_directly`` — wants a *lot* of the log to classify the error.
  It runs its own bounded LLM call **in-process** and returns only the
  structured analysis, so the raw log never enters chat history.
* the agent's chat history — wants a *short* excerpt, because the structured
  analysis already carries the actionable part.

So this module bounds only what crosses the agent boundary and never touches
the text handed to the analyzer.

Head+tail rather than head-only: in a Spark log the root cause (the thrown
exception) sits near the head, but drivers sometimes append the decisive
"Job aborted due to stage failure" chain near the tail.
"""

from __future__ import annotations

from typing import Any

from dataagent.utils.log import logger

# How much of the excerpt goes to the head. The head carries the exception
# class/message and the top of the stack, which is where the root cause lives.
_HEAD_RATIO = 0.7

# Error text is prose, not a data payload: a couple of KB is well past what
# the agent needs to recognise the failure mode, and the analyzer has already
# distilled it further by the time the agent sees anything.
DEFAULT_ERROR_MAX_CHARS = 2_000

# Payload keys that carry backend error prose and can therefore be huge.
_ERROR_FIELDS = ("error", "summary", "message", "reason")
_ERROR_LOG_FIELD = "errorLog"


def bound_text(text: Any, *, max_chars: int, label: str = "text") -> Any:
    """Cap ``text`` at ``max_chars``, keeping head and tail with a marker.

    Non-strings pass through untouched so callers can apply this to a payload
    dict without first type-checking every value.
    """
    if not isinstance(text, str) or len(text) <= max_chars:
        return text

    head_len = int(max_chars * _HEAD_RATIO)
    tail_len = max_chars - head_len
    omitted = len(text) - max_chars
    return (
        text[:head_len]
        + f"\n\n...[{label} truncated: omitted middle {omitted} chars]...\n\n"
        + text[-tail_len:]
    )


def bound_error_fields(payload: dict[str, Any], *, max_chars: int = DEFAULT_ERROR_MAX_CHARS) -> dict[str, Any]:
    """Bound the error-prose fields of ``payload`` **in place**; return it.

    Bounds ``error``/``summary``/``message``/``reason``/``errorLog``. Safe to
    call on any DataOps result dict — unknown keys are left alone, and fields
    already short enough are returned unchanged.
    """
    if not isinstance(payload, dict):
        return payload

    for key in _ERROR_FIELDS:
        if key in payload:
            payload[key] = bound_text(payload[key], max_chars=max_chars, label=key)

    if _ERROR_LOG_FIELD in payload:
        payload[_ERROR_LOG_FIELD] = bound_text(
            payload[_ERROR_LOG_FIELD], max_chars=max_chars, label=_ERROR_LOG_FIELD
        )

    logger.debug(
        f"[dataops_log_utils] bounded error fields max_chars={max_chars} keys={sorted(payload.keys())}"
    )
    return payload


def bound_error_excerpt(text: Any, *, max_chars: int = DEFAULT_ERROR_MAX_CHARS, label: str = "error") -> Any:
    """Bound a single error string (used for summaries echoed across retries)."""
    return bound_text(text, max_chars=max_chars, label=label)
