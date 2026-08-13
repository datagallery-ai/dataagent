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

import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextvars import Token
from functools import partial
from pathlib import Path
from typing import Any, Optional, cast

from dataagent.agents.nl2sql.context_recorder import NL2SQLContextRecorder
from dataagent.agents.nl2sql.errors import NL2SQLError
from dataagent.agents.nl2sql.nodes import (
    BaseNL2SQLNode,
    BusinessTwinPerceptorNode,
    ExecutorNode,
    GeneratorNode,
    PerceptorNode,
    ReflectorNode,
    SelectorNode,
    ValidatorNode,
)
from dataagent.agents.nl2sql.workflow.router import NL2SQLRouter
from dataagent.agents.nl2sql.workflow.state import NL2SQLState, get_default_state
from dataagent.core.cbb.base_agent import BaseAgent
from dataagent.core.framework_adapters.runtime.workflow_backend_factory import create_workflow_backend
from dataagent.core.utils.performance import make_perf_state_holder, update_latest_state_from_stream_item
from dataagent.utils.constants import DEFAULT_NL2SQL_REF_RETRIES, DEFAULT_NL2SQL_SEL_RETRIES
from dataagent.utils.log import logger


class NL2SQLAgent(BaseAgent):
    def __init__(
        self,
        *,
        backend: str,
        nodes: list[BaseNL2SQLNode],
        router: NL2SQLRouter,
        config: Any,
        state_defaults: dict[str, Any] | None = None,
        config_manager: Optional[Any] = None,
    ):
        """Initialize an NL2SQL agent and attach Context trajectory hooks."""
        self._config_obj = config
        cfg_dict = {}
        try:
            if isinstance(config, dict):
                cfg_dict = dict(config)
            elif hasattr(config, "settings") and isinstance(getattr(config, "settings", None), dict):
                cfg_dict = dict(config.settings)
        except Exception:
            cfg_dict = {}
        super().__init__(config=cfg_dict)
        self.backend = backend
        self.router = router
        self.nodes = nodes
        self.config_manager = config_manager
        self.sql_security_enabled = any(
            isinstance(node, ValidatorNode) and node.sql_security_enabled for node in self.nodes
        )
        self._context_recording_enabled = True
        for node in self.nodes:
            node.add_post_hook(partial(NL2SQLContextRecorder.record_action_hook, node_name=node.name))
        self.workflow_backend = create_workflow_backend(
            backend=backend,
            nodes=list(self.nodes),
            router=self.router,
            state_class=NL2SQLState,
            config=self._config_obj,
        )
        self.state_defaults = state_defaults or {}

    @staticmethod
    def _finish_context_recorder(
        *,
        recorder: Optional[NL2SQLContextRecorder],
        token: Optional[Token[Optional[NL2SQLContextRecorder]]],
        final_state: Optional[Mapping[str, Any]],
        completed: bool,
    ) -> None:
        if recorder is None or token is None:
            return
        try:
            try:
                recorder.finish(final_state=final_state, completed=completed)
            except Exception as exc:
                logger.warning(f"Failed to finish NL2SQL Context recorder: {exc}")
        finally:
            try:
                recorder.reset(token)
            except Exception as exc:
                logger.warning(f"Failed to reset NL2SQL Context recorder: {exc}")

    @classmethod
    def from_config(cls, config: Any, config_manager: Any | None = None) -> NL2SQLAgent:
        """Build an NL2SQL agent from its YAML-compatible configuration."""
        core_cfg = config.get("CORE", {})
        db_cfg = config.get("DATABASE", {})
        validator_cfg = core_cfg.get("validator", {}) or {}
        security_enabled = bool(validator_cfg.get("sql_security_enabled", False))
        if security_enabled and "reflector" not in core_cfg:
            raise ValueError("CORE.reflector is required when CORE.validator.sql_security_enabled is true.")
        perceptor_cls = BusinessTwinPerceptorNode if db_cfg.get("db_id") == "business_twin" else PerceptorNode
        node_chain = [
            ("perceptor", perceptor_cls, {}),
            ("generator", GeneratorNode, {}),
            ("validator", ValidatorNode, {}),
            ("reflector", ReflectorNode, {"ref_retries": DEFAULT_NL2SQL_REF_RETRIES}),
            ("executor", ExecutorNode, {}),
            ("selector", SelectorNode, {"sel_retries": DEFAULT_NL2SQL_SEL_RETRIES}),
        ]
        enabled_nodes: list[str] = []
        node_instances: list[BaseNL2SQLNode] = []
        state_defaults: dict[str, Any] = {}
        for name, node_cls, default_state in node_chain:
            if name not in core_cfg:
                break
            enabled_nodes.append(name)
            node_cfg = dict(core_cfg.get(name, {}) or {})
            if name == "reflector" and security_enabled:
                node_cfg.update({"sql_security_enabled": True})
            for state_key, state_value in default_state.items():
                state_defaults[state_key] = node_cfg.get(state_key, state_value)
            if config_manager is not None:
                node_cfg["config_manager"] = config_manager
            node_instances.append(node_cls(**node_cfg))
        if "generator" not in enabled_nodes:
            raise ValueError("Perceptor and Generator are required in the yaml.")
        router = NL2SQLRouter(enabled_nodes)
        return cls(
            backend="langgraph",
            nodes=node_instances,
            router=router,
            config=config,
            state_defaults=state_defaults,
            config_manager=config_manager,
        )

    async def chat(self, message: str, initial_state: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Run one NL2SQL chat turn."""
        try:
            checkpoint_id: str | None = kwargs.pop("checkpoint_id", None)
            session_id: str | None = kwargs.pop("session_id", None)
            init = initial_state or kwargs.pop("initial_state", None) or {}
            if checkpoint_id:
                recorder, token = self._start_context_recorder(state=init, question=message, session_id=session_id)
                final_state: Optional[dict[str, Any]] = None
                completed = False
                try:
                    final_state = await self.workflow_backend.resume(
                        checkpoint_id=str(checkpoint_id), message=message, session_id=session_id, **kwargs
                    )
                    completed = True
                    return final_state
                finally:
                    self._finish_context_recorder(
                        recorder=recorder,
                        token=token,
                        final_state=final_state,
                        completed=completed,
                    )
            if not session_id:
                session_id = str(uuid.uuid4())
            state = get_default_state(question=message, **{**self.state_defaults, **(init or {})})
            self._distribute_context_dump_dir(init, session_id=session_id)
            latest, flush_provider = make_perf_state_holder(state)
            recorder, token = self._start_context_recorder(state=state, question=message, session_id=session_id)
            final_state = None
            completed = False
            try:
                with self._performance_run(state=state, backend=self.backend, flush_state_provider=flush_provider):
                    final_state = await self.workflow_backend.ainvoke(state)
                    if isinstance(final_state, dict):
                        latest["state"] = final_state
                completed = True
                return final_state
            finally:
                self._finish_context_recorder(
                    recorder=recorder,
                    token=token,
                    final_state=final_state,
                    completed=completed,
                )
        except NL2SQLError as exc:
            return {"error": exc.to_dict()}
        except Exception as exc:
            return {"error": {"message": str(exc), "type": exc.__class__.__name__}}

    def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        """Stream NL2SQL workflow via LangGraph native astream."""

        async def _gen() -> AsyncGenerator[Any, None]:
            try:
                kw = dict(kwargs)

                input_state = kw.get("input")
                if isinstance(input_state, dict):
                    question = str(input_state.get("question") or input_state.get("user_query", ""))
                    async for item in self._yield_context_stream(
                        state=input_state,
                        question=question,
                        session_id=input_state.get("session_id"),
                        stream=self.workflow_backend.astream({}, **kw),
                    ):
                        yield item
                    return

                initial_state = kw.pop("initial_state", None)
                start_at = kw.pop("start_at", None)
                checkpoint_id = kw.pop("checkpoint_id", None)
                message = kw.pop("message", None)
                session_id = kw.pop("session_id", None)
                stream_mode = kw.pop("stream_mode", ["updates", "custom", "values"])

                if checkpoint_id:
                    perf_state: dict[str, Any] = dict(initial_state) if isinstance(initial_state, dict) else {}
                    if session_id:
                        perf_state.setdefault("session_id", session_id)
                    async for item in self._yield_context_stream(
                        state=perf_state,
                        question=str(message or ""),
                        session_id=session_id,
                        stream=self.workflow_backend.astream_resume(
                            checkpoint_id=str(checkpoint_id),
                            message=str(message or ""),
                            session_id=session_id,
                            stream_mode=stream_mode,
                            **kw,
                        ),
                    ):
                        yield item
                    return

                if args and isinstance(args[0], dict) and initial_state is None:
                    initial_state = args[0]
                if not isinstance(initial_state, dict):
                    initial_state = {}
                if args and not isinstance(args[0], dict) and message is None:
                    message = args[0]
                if not session_id:
                    session_id = str(uuid.uuid4())

                question = str(message or initial_state.pop("question", None) or initial_state.pop("user_query", ""))
                initial_state.setdefault("session_id", session_id)
                state = get_default_state(question=question, **{**self.state_defaults, **(initial_state or {})})
                async for item in self._yield_context_stream(
                    state=state,
                    question=question,
                    session_id=session_id,
                    stream=self.workflow_backend.astream(
                        cast(dict[str, Any], state),
                        start_at=start_at,
                        stream_mode=stream_mode,
                        **kw,
                    ),
                ):
                    yield item
            except NL2SQLError as exc:
                yield {"error": exc.to_dict()}
            except Exception as exc:
                yield {"error": {"message": str(exc), "type": exc.__class__.__name__}}

        return _gen()

    def _start_context_recorder(
        self,
        *,
        state: Mapping[str, Any],
        question: str,
        session_id: Optional[str],
    ) -> tuple[
        Optional[NL2SQLContextRecorder],
        Optional[Token[Optional[NL2SQLContextRecorder]]],
    ]:
        if not getattr(self, "_context_recording_enabled", False):
            return None, None
        config = self.config if isinstance(getattr(self, "config", None), Mapping) else {}
        recorder = NL2SQLContextRecorder.create(
            state=state,
            question=question,
            session_id=session_id,
            config=config,
            config_manager=getattr(self, "config_manager", None),
        )
        if recorder is None:
            return None, None
        return recorder, recorder.bind()

    async def _yield_context_stream(
        self,
        *,
        state: Mapping[str, Any],
        question: str,
        session_id: Optional[str],
        stream: AsyncIterator[Any],
    ) -> AsyncGenerator[Any, None]:
        recorder, token = self._start_context_recorder(state=state, question=question, session_id=session_id)
        completed = True
        try:
            async for item in self._yield_perf_stream(state, stream):
                yield item
        except BaseException:
            completed = False
            raise
        finally:
            self._finish_context_recorder(
                recorder=recorder,
                token=token,
                final_state=None,
                completed=completed,
            )

    def _distribute_context_dump_dir(self, init: dict[str, Any], *, session_id: str | None = None) -> None:
        """Resolve and distribute the per-run NL2SQL context-dump dir to all nodes."""
        from dataagent.utils.env_utils import get_env_bool

        if not get_env_bool("DATAAGENT_CONTEXT_DUMP"):
            return

        user_id = str(init.get("user_id") or "anonymous")
        cfg = self._config_obj
        cfg_session_id = cfg.get("SESSION_ID") if isinstance(cfg, dict) else None
        parent_session_id = init.get("_parent_session_id")
        if cfg_session_id:
            effective_session_id = str(cfg_session_id)
        elif parent_session_id:
            effective_session_id = str(parent_session_id)
        elif session_id:
            effective_session_id = str(session_id)
        else:
            effective_session_id = str(init.get("session_id") or "default_session")
        run_id = init.get("_parent_run_id", init.get("run_id", 0))
        try:
            dump_dir = self._create_context_dump_dir(
                user_id=user_id,
                session_id=effective_session_id,
                workspace=init.get("workspace"),
                run_id=run_id,
            )
        except Exception as exc:
            logger.warning(f"Failed to init NL2SQL context dump dir: {exc}")
            return
        logger.info(
            f"[_distribute_context_dump_dir] session_id={effective_session_id}, "
            f"user_id={user_id}, run_id={run_id}, dump_dir={dump_dir}"
        )
        shared_seq: list[int] = [0]
        for node in self.nodes:
            node.set_context_dump_dir(dump_dir)
            node._context_dump_seq = shared_seq
        logger.info(f"[_distribute_context_dump_dir] distributed dump_dir to {len(self.nodes)} nodes")

    def _create_context_dump_dir(
        self,
        *,
        user_id: str,
        session_id: str,
        workspace: Any,
        run_id: Any,
    ) -> Path:
        """Create and return the next per-run NL2SQL context-dump directory."""
        from dataagent.utils.runtime_paths import resolve_flex_session_memory_dir

        memory_dir = resolve_flex_session_memory_dir(
            user_id=user_id,
            session_id=session_id,
            workspace=workspace,
            config=self.config,
        )
        base_dir = memory_dir / "context_dump" / f"run_{run_id}"
        existing = (
            [path.name for path in base_dir.iterdir() if path.is_dir() and path.name.startswith("nl2sql_")]
            if base_dir.is_dir()
            else []
        )
        dump_dir = base_dir / f"nl2sql_{len(existing) + 1:02d}"
        dump_dir.mkdir(parents=True, exist_ok=True)
        return dump_dir

    async def _yield_perf_stream(
        self,
        state: Mapping[str, Any] | None,
        stream: AsyncIterator[Any],
    ) -> AsyncGenerator[Any, None]:
        latest, flush_provider = make_perf_state_holder(state)
        with self._performance_run(state=state, backend=self.backend, flush_state_provider=flush_provider):
            async for item in stream:
                update_latest_state_from_stream_item(item, latest)
                if self.sql_security_enabled:
                    from dataagent.agents.nl2sql.security.streaming import sanitize_stream_item

                    item = sanitize_stream_item(item)
                yield item
