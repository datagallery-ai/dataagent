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

import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from typing import Any

from dataagent.agents.nl2sql.errors import NL2SQLError
from dataagent.agents.nl2sql.nodes import (
    BaseNL2SQLNode,
    CoordinatorNode,
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
from dataagent.utils.log import logger


class NL2SQLAgent(BaseAgent):
    def __init__(self, *, backend: str, nodes: list[BaseNL2SQLNode], router: NL2SQLRouter, config: Any):
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
        self.workflow_backend = create_workflow_backend(
            backend=backend,
            nodes=list(self.nodes),
            router=self.router,
            state_class=NL2SQLState,
            config=self._config_obj,
        )

    @classmethod
    def from_config(cls, config: Any, config_manager: Any | None = None) -> NL2SQLAgent:
        core_cfg = config.get("CORE", {})
        node_chain = [
            ("coordinator", CoordinatorNode),
            ("perceptor", PerceptorNode),
            ("generator", GeneratorNode),
            ("validator", ValidatorNode),
            ("reflector", ReflectorNode),
            ("executor", ExecutorNode),
            ("selector", SelectorNode),
        ]
        enabled_nodes: list[str] = []
        node_instances: list[BaseNL2SQLNode] = []
        for name, node_cls in node_chain:
            if name not in core_cfg:
                break
            enabled_nodes.append(name)
            node_kwargs = dict(core_cfg.get(name, {}) or {})
            if config_manager is not None:
                node_kwargs["config_manager"] = config_manager
            node_instances.append(node_cls(**node_kwargs))
        if "generator" not in enabled_nodes:
            raise ValueError("Coordinator, Perceptor, and Generator are required in the yaml.")
        router = NL2SQLRouter(enabled_nodes)
        return cls(backend="langgraph", nodes=node_instances, router=router, config=config)

    def _distribute_context_dump_dir(
        self, init: dict[str, Any], *, session_id: str | None = None
    ) -> None:
        from dataagent.utils.env_utils import get_env_bool
        if not get_env_bool("DATAAGENT_CONTEXT_DUMP"):
            return
        try:
            from dataagent.utils.runtime_paths import resolve_session_root
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
            base_dir = (
                resolve_session_root(user_id=user_id, session_id=effective_session_id)
                / ".memory"
                / "context_dump"
                / f"run_{run_id}"
            )
            existing = [d.name for d in base_dir.iterdir() if d.is_dir() and d.name.startswith("nl2sql_")] if base_dir.is_dir() else []
            next_idx = len(existing) + 1
            dump_dir = base_dir / f"nl2sql_{next_idx:02d}"
            logger.info(f"[_distribute_context_dump_dir] session_id={effective_session_id}, user_id={user_id}, run_id={run_id}, dump_dir={dump_dir}")
            dump_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(f"Failed to init NL2SQL context dump dir: {exc}")
            return
        shared_seq: list[int] = [0]
        for node in self.nodes:
            node.set_context_dump_dir(dump_dir)
            node._context_dump_seq = shared_seq
        logger.info(f"[_distribute_context_dump_dir] distributed dump_dir to {len(self.nodes)} nodes")

    async def chat(self, message: str, initial_state: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Run one NL2SQL chat turn."""
        try:
            checkpoint_id: str | None = kwargs.pop("checkpoint_id", None)
            session_id: str | None = kwargs.pop("session_id", None)
            if checkpoint_id:
                return await self.workflow_backend.resume(
                    checkpoint_id=str(checkpoint_id), message=message, session_id=session_id, **kwargs
                )
            if not session_id:
                session_id = str(uuid.uuid4())
            init = initial_state or kwargs.pop("initial_state", None) or {}
            state = get_default_state(question=message, **init)
            self._distribute_context_dump_dir(init, session_id=session_id)
            latest, flush_provider = make_perf_state_holder(state)
            with self._performance_run(state=state, backend=self.backend, flush_state_provider=flush_provider):
                final_state = await self.workflow_backend.ainvoke(state)
                if isinstance(final_state, dict):
                    latest["state"] = final_state
            return final_state
        except NL2SQLError as exc:
            return {"error": exc.to_dict()}
        except Exception as exc:
            return {"error": {"message": str(exc), "type": exc.__class__.__name__}}

    def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        """Stream NL2SQL workflow via LangGraph native astream."""

        async def _gen() -> AsyncGenerator[Any, None]:
            try:
                kw = dict(kwargs)

                if "input" in kw and isinstance(kw["input"], dict):
                    async for item in self._yield_perf_stream(kw["input"], self.workflow_backend.astream({}, **kw)):
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
                    async for item in self._yield_perf_stream(
                        perf_state,
                        self.workflow_backend.astream_resume(
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
                state = get_default_state(question=question, **initial_state)
                async for item in self._yield_perf_stream(
                    state,
                    self.workflow_backend.astream(state, start_at=start_at, stream_mode=stream_mode, **kw),
                ):
                    yield item
            except NL2SQLError as exc:
                yield {"error": exc.to_dict()}
            except Exception as exc:
                yield {"error": {"message": str(exc), "type": exc.__class__.__name__}}

        return _gen()

    async def _yield_perf_stream(
        self,
        state: Mapping[str, Any] | None,
        stream: AsyncIterator[Any],
    ) -> AsyncGenerator[Any, None]:
        latest, flush_provider = make_perf_state_holder(state)
        with self._performance_run(state=state, backend=self.backend, flush_state_provider=flush_provider):
            async for item in stream:
                update_latest_state_from_stream_item(item, latest)
                yield item
