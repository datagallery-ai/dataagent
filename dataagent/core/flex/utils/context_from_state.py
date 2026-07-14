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
"""Resolve :class:`~dataagent.core.context.context.Context` from Flex workflow state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from loguru import logger

from dataagent.core.context.context import Context, ContextFactory, build_context_init_options


def get_context_for_flex_state(
    state: Mapping[str, Any],
    runtime: Any = None,
    *,
    swallow_errors: bool = False,
) -> Context | None:
    """
    Get or create the cached Context for identifiers in ``state``.

    When ``runtime`` is provided, PRE/POST workflow and database URL options are taken from
    ``runtime.config_manager`` via :func:`~dataagent.core.context.context.build_context_init_options`.

    Args:
        state: Flex state containing ``user_id``, ``session_id``, ``run_id``, and ``sub_id``.
        runtime: Per-invocation :class:`~dataagent.core.cbb.runtime.Runtime` from workflow.
        swallow_errors: If True, log and return ``None`` on failure (pruner hook). If False, propagate.

    Returns:
        Context instance, or ``None`` when ids are missing or ``swallow_errors`` is True and lookup fails.
    """
    try:
        user_id = str(state.get("user_id", "") or "")
        session_id = str(state.get("session_id", "") or "")
        if not user_id or not session_id:
            return None
        run_id = int(state.get("run_id", 0) or 0)
        sub_id = int(state.get("sub_id", 0) or 0)
        options = None
        if runtime is not None:
            cm = runtime.config_manager
            if cm is not None:
                options = build_context_init_options(cm, workspace=state.get("workspace"))
        return ContextFactory.get_context(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            sub_id=sub_id,
            options=options,
        )
    except Exception as exc:
        if swallow_errors:
            logger.debug(f"get_context_for_flex_state failed: {exc}")
            return None
        raise
