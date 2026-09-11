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

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextvars import Token
from functools import partial
from pathlib import Path
from typing import Any, Optional, cast

from dataagent.agents.bird.constants import DEFAULT_BIRD_REF_RETRIES
from dataagent.agents.bird.context_dump import (
    BirdContextDump,
    context_dump_scope,
    stream_without_context_dump,
    validate_bird_path_inputs,
)
from dataagent.agents.bird.context_recorder import BirdContextRecorder
from dataagent.agents.bird.nodes import (
    BaseBirdNode,
    ExecutorNode,
    GeneratorNode,
    PerceptorNode,
    ReflectorNode,
    SelectorNode,
    ValidatorNode,
)
from dataagent.agents.bird.workflow.router import BirdRouter
from dataagent.agents.bird.workflow.state import BirdState, get_default_state
from dataagent.core.cbb.base_agent import BaseAgent
from dataagent.core.framework_adapters.runtime.workflow_backend_factory import create_workflow_backend
from dataagent.core.utils.performance import make_perf_state_holder, update_latest_state_from_stream_item
from dataagent.utils.log import logger


class BirdAgent(BaseAgent):
    def __init__(
        self,
        *,
        backend: str,
        nodes: list[BaseBirdNode],
        router: BirdRouter,
        config: Any,
        sql_security_enabled: bool = False,
        state_defaults: dict[str, Any] | None = None,
        config_manager: Optional[Any] = None,
    ):
        """Initialize an BIRD agent and attach Context trajectory hooks."""
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
        llm_limit = cfg_dict.get("CORE", {}).get("llm_max_concurrency")
        if llm_limit is not None and (type(llm_limit) is not int or llm_limit < 1):
            raise ValueError("CORE.llm_max_concurrency must be a positive integer or null")
        self._llm_limiter = asyncio.Semaphore(llm_limit) if llm_limit is not None else None
        self.backend = backend
        self.router = router
        self.nodes = nodes
        self.config_manager = config_manager
        self.sql_security_enabled = bool(sql_security_enabled)
        self._context_recording_enabled = True
        for node in self.nodes:
            node._llm_limiter = self._llm_limiter
            node.add_post_hook(partial(BirdContextRecorder.record_action_hook, node_name=node.name))
        self.workflow_backend = create_workflow_backend(
            backend=backend,
            nodes=list(self.nodes),
            router=self.router,
            state_class=BirdState,
            config=self._config_obj,
        )
        self.state_defaults = state_defaults or {}

    @staticmethod
    def _finish_context_recorder(
        *,
        recorder: Optional[BirdContextRecorder],
        token: Optional[Token[Optional[BirdContextRecorder]]],
        final_state: Optional[Mapping[str, Any]],
        completed: bool,
    ) -> None:
        if recorder is None or token is None:
            return
        try:
            try:
                recorder.finish(final_state=final_state, completed=completed)
            except Exception as exc:
                logger.warning(f"Failed to finish BIRD Context recorder: {exc}")
        finally:
            try:
                recorder.reset(token)
            except Exception as exc:
                logger.warning(f"Failed to reset BIRD Context recorder: {exc}")

    @classmethod
    def from_config(
        cls,
        config: Any,
        config_manager: Any | None = None,
    ) -> BirdAgent:
        """Build an BIRD agent from its YAML-compatible configuration."""
        core_cfg = config.get("CORE", {})
        required_nodes = ("perceptor", "generator", "validator", "reflector", "executor", "selector")
        missing_nodes = [name for name in required_nodes if name not in core_cfg]
        if missing_nodes:
            raise ValueError(f"BIRD requires the fixed six-node CORE chain; missing: {', '.join(missing_nodes)}")
        validator_cfg = core_cfg.get("validator", {}) or {}
        security_enabled = bool(validator_cfg.get("sql_security_enabled", False))
        reflector_cfg = core_cfg.get("reflector", {}) or {}
        if "sql_security_enabled" in reflector_cfg and bool(reflector_cfg["sql_security_enabled"]) != security_enabled:
            raise ValueError(
                "CORE.reflector.sql_security_enabled must match "
                "CORE.validator.sql_security_enabled; validator is the BIRD security source of truth."
            )
        node_chain = [
            ("perceptor", PerceptorNode, {}),
            ("generator", GeneratorNode, {}),
            ("validator", ValidatorNode, {}),
            ("reflector", ReflectorNode, {"ref_retries": DEFAULT_BIRD_REF_RETRIES}),
            ("executor", ExecutorNode, {}),
            ("selector", SelectorNode, {}),
        ]
        node_instances: list[BaseBirdNode] = []
        state_defaults: dict[str, Any] = {}
        for name, node_cls, default_state in node_chain:
            node_cfg = dict(core_cfg.get(name, {}) or {})
            if name == "reflector":
                node_cfg["sql_security_enabled"] = security_enabled
            for state_key, state_value in default_state.items():
                state_defaults[state_key] = node_cfg.get(state_key, state_value)
            if config_manager is not None:
                node_cfg["config_manager"] = config_manager
            node_instances.append(node_cls(**node_cfg))
        router = BirdRouter()
        return cls(
            backend="langgraph",
            nodes=node_instances,
            router=router,
            config=config,
            sql_security_enabled=security_enabled,
            state_defaults=state_defaults,
            config_manager=config_manager,
        )

    async def chat(self, message: str, initial_state: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Run one BIRD chat turn."""
        init = initial_state or {}
        session_id = kwargs.get("session_id")
        if not session_id and not kwargs.get("checkpoint_id"):
            session_id = str(uuid.uuid4())
            kwargs["session_id"] = session_id
        validate_bird_path_inputs(self.config, {**self.state_defaults, **init}, session_id=session_id)
        dump = None if kwargs.get("checkpoint_id") else self._prepare_context_dump(init, session_id=session_id)
        with context_dump_scope(dump):
            return await self._chat(message, initial_state=init, **kwargs)

    async def _chat(self, message: str, initial_state: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
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

    def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        """Stream BIRD workflow via LangGraph native astream."""

        async def _gen() -> AsyncGenerator[Any, None]:
            kw = dict(kwargs)
            validate_bird_path_inputs(self.config, kw.get("initial_state"), session_id=kw.get("session_id"))
            validate_bird_path_inputs(self.config, kw.get("input"))
            if args and isinstance(args[0], dict):
                validate_bird_path_inputs(self.config, args[0])

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
            initial_state = {} if not isinstance(initial_state, dict) else dict(initial_state)
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

        return _gen()

    def _start_context_recorder(
        self,
        *,
        state: Mapping[str, Any],
        question: str,
        session_id: Optional[str],
    ) -> tuple[
        Optional[BirdContextRecorder],
        Optional[Token[Optional[BirdContextRecorder]]],
    ]:
        if not getattr(self, "_context_recording_enabled", False):
            return None, None
        config = self.config if isinstance(getattr(self, "config", None), Mapping) else {}
        recorder = BirdContextRecorder.create(
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
            async for item in stream_without_context_dump(self._yield_perf_stream(state, stream)):
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

    def _prepare_context_dump(self, init: dict[str, Any], *, session_id: str | None = None) -> BirdContextDump | None:
        """Resolve diagnostics for this call without mutating shared nodes."""
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
            logger.warning(f"Failed to init BIRD context dump dir: {exc}")
            return
        logger.info(
            f"[_prepare_context_dump] session_id={effective_session_id}, "
            f"user_id={user_id}, run_id={run_id}, dump_dir={dump_dir}"
        )
        return BirdContextDump(dump_dir)

    def _create_context_dump_dir(
        self,
        *,
        user_id: str,
        session_id: str,
        workspace: Any,
        run_id: Any,
    ) -> Path:
        """Create and return the next per-run BIRD context-dump directory."""
        from dataagent.utils.runtime_paths import resolve_flex_session_memory_dir, resolve_flex_storage_root

        validate_bird_path_inputs(self.config, {"session_id": session_id, "run_id": run_id})
        path_args = {"user_id": user_id, "session_id": session_id, "workspace": workspace, "config": self.config}
        storage_root = resolve_flex_storage_root(**path_args).resolve()
        memory_dir = resolve_flex_session_memory_dir(**path_args).resolve()
        context_root = (memory_dir / "context_dump").resolve()
        base_dir = (context_root / f"run_{run_id}").resolve()
        if (
            not memory_dir.is_relative_to(storage_root)
            or not context_root.is_relative_to(memory_dir)
            or not base_dir.is_relative_to(context_root)
        ):
            raise ValueError("Bird context dump must remain inside the session memory directory.")
        existing = (
            [path.name for path in base_dir.iterdir() if path.is_dir() and path.name.startswith("bird_")]
            if base_dir.is_dir()
            else []
        )
        index = len(existing) + 1
        while True:
            dump_dir = (base_dir / f"bird_{index:02d}").resolve()
            if not dump_dir.is_relative_to(base_dir):
                raise ValueError("Bird context dump must remain inside its run directory.")
            try:
                dump_dir.mkdir(parents=True, exist_ok=False)
                return dump_dir
            except FileExistsError:
                index += 1

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
                    from dataagent.agents.bird.security.streaming import sanitize_stream_item

                    item = sanitize_stream_item(item)
                yield item
