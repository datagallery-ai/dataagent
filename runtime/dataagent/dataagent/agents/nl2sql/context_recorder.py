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
# ruff: noqa: UP045

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar, Token
from typing import Any, Optional

from dataagent.core.context.context import (
    Context,
    ContextFactory,
    ContextInitOptions,
    build_context_init_options,
)
from dataagent.utils.log import logger

_CURRENT_RECORDER: ContextVar[Optional[NL2SQLContextRecorder]] = ContextVar(
    "current_nl2sql_context_recorder", default=None
)


class NL2SQLContextRecorder:
    """Record the current NL2SQL run in the shared Context trajectory DAG."""

    def __init__(self, context: Context) -> None:
        """Initialize a recorder for one Context run."""
        self._context = context
        self._latest_state: Optional[Mapping[str, Any]] = None
        self._finished = False

    @staticmethod
    def record_action_hook(
        state: Mapping[str, Any],
        runtime: Any = None,
        *,
        node_name: str,
    ) -> Mapping[str, Any]:
        """Record one successfully completed NL2SQL workflow node."""
        _ = runtime
        recorder = _CURRENT_RECORDER.get()
        if recorder is not None:
            recorder.record_action(node_name=node_name, state=state)
        return state

    @staticmethod
    def reset(token: Token[Optional[NL2SQLContextRecorder]]) -> None:
        """Restore the previous recorder binding."""
        _CURRENT_RECORDER.reset(token)

    @classmethod
    def create(
        cls,
        *,
        state: Mapping[str, Any],
        question: str,
        session_id: Optional[str],
        config: Mapping[str, Any],
        config_manager: Optional[Any] = None,
    ) -> Optional[NL2SQLContextRecorder]:
        """Create a recorder and register the current run's Query node."""
        try:
            workspace = state.get("workspace")
            if not workspace:
                workspace_config = config.get("WORKSPACE", {})
                if isinstance(workspace_config, Mapping):
                    workspace = workspace_config.get("path")
            options = (
                build_context_init_options(config_manager, workspace=workspace)
                if config_manager is not None
                else ContextInitOptions(workspace=workspace, config=config)
            )
            user_id = str(state.get("user_id", config.get("USER_ID", "anonymous")) or "anonymous")
            effective_session_id = str(
                state.get("session_id")
                or session_id
                or config.get("SESSION_ID", "default_session")
                or "default_session"
            )
            run_id = int(state.get("run_id", config.get("RUN_ID", 0)) or 0)
            sub_id = int(state.get("sub_id", config.get("SUB_ID", 0)) or 0)
            context = ContextFactory.get_context(
                user_id=user_id,
                session_id=effective_session_id,
                run_id=run_id,
                sub_id=sub_id,
                options=options,
            )
            if not context.has_initial_pt:
                context.register_query(query=question, additional_files=[])
            return cls(context)
        except Exception as exc:
            logger.warning(f"Failed to initialize NL2SQL Context recorder: {exc}")
            return None

    def bind(self) -> Token[Optional[NL2SQLContextRecorder]]:
        """Bind this recorder to the current asynchronous call context."""
        return _CURRENT_RECORDER.set(self)

    def record_action(self, *, node_name: str, state: Mapping[str, Any]) -> None:
        """Append one minimal Action node without changing NL2SQL state."""
        self._latest_state = state
        try:
            self._context.register_node(
                node_type="Action",
                description=f"NL2SQL node: {node_name}",
                predecessor_node=list(self._context.state.current_pt),
                edge_type="triggers",
                action=node_name,
                params={},
                output="",
                success=True,
            )
        except Exception as exc:
            logger.warning(f"Failed to record NL2SQL Context action {node_name}: {exc}")

    def finish(self, *, final_state: Optional[Mapping[str, Any]], completed: bool) -> None:
        """Optionally record the final Response and persist the current trajectory."""
        if self._finished:
            return
        self._finished = True
        state = final_state if final_state is not None else self._latest_state
        if completed and state is not None and not state.get("error"):
            self._register_response(state)
        try:
            self._context.persist_to_json()
        except Exception as exc:
            logger.warning(f"Failed to persist NL2SQL Context trajectory: {exc}")

    def _register_response(self, state: Mapping[str, Any]) -> None:
        try:
            self._context.register_node(
                node_type="Response",
                description="NL2SQL response",
                predecessor_node=list(self._context.state.current_pt),
                response=str(state.get("sql", "") or ""),
                reasoning_content="",
            )
        except Exception as exc:
            logger.warning(f"Failed to record NL2SQL Context response: {exc}")
