# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""DataAgent — 基于 openjiuwen DeepAgent 的数据分析 Agent。

唯一公开入口::

    agent = DataAgent.from_config("path/to/config.yaml")
    response = await agent.chat("分析销售数据")
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dataagent.config import ConfigManager
from dataagent.core.deep_agent import (
    CheckpointerSpec,
    DeepAgentAdapter,
    build_checkpointer_spec,
    build_interactive_input,
    build_model_from_config,
    build_system_prompt,
    checkpointer_lease,
)
from dataagent.utils.log import logger
from dataagent.utils.runtime_paths import dataagent_package_path, resolve_effective_workspace_root

if TYPE_CHECKING:
    from openjiuwen.core.session.agent import Session
    from openjiuwen.harness import DeepAgent

    from dataagent.core.deep_agent import A2AAgentBinding, SkillRailBinding


class DataAgent:
    """DataAgent — 基于 DeepAgent 的智能数据分析助手。

    用法::

        agent = DataAgent.from_config("config.yaml")
        result = await agent.chat("帮我分析数据")
        async for chunk in agent.astream(initial_state={"user_query": "..."}):
            ...
    """

    def __init__(self, config: ConfigManager):
        self.config = config
        self._deep_agent: DeepAgent | None = None
        self._a2a_agents: list[A2AAgentBinding] = []
        self._a2a_cleanup_tasks: set[asyncio.Task] = set()
        self._skill_binding: SkillRailBinding | None = None
        self._session: Session | None = None
        self._checkpointer_spec: CheckpointerSpec | None = None
        self.session_id: str | None = None
        self.type = config.get("AGENT_CONFIG.type", "react") if hasattr(config, "get") else "react"
        self.agent_type = config.get("AGENT_CONFIG.agent_type") if hasattr(config, "get") else None
        self.backend = "openjiuwen"

    def __repr__(self) -> str:
        return f"DataAgent(type={self.type}, backend={self.backend})"

    # ── 构建 ────────────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: str | Path) -> DataAgent:
        """从 YAML 配置文件创建 Agent。

        加载优先级（后者覆盖前者）：
        1. ``flex_default_configs.yaml``（默认兜底）
        2. 用户指定的 YAML 配置文件
        3. ``.env`` 环境变量（最高优先级）
        """
        # Silence jiuwen's INFO logs — must happen after jiuwen is loaded
        from dataagent import _silence_jiuwen_logging

        _silence_jiuwen_logging()

        default_config_path: str | None = None
        candidate = dataagent_package_path("core", "flex", "flex_default_configs.yaml")
        if candidate.exists():
            default_config_path = str(candidate)

        cm = ConfigManager()
        cm.reload(str(config), default_config_path=default_config_path)

        if cm.get("AGENT_CONFIG.type") is None:
            cm.set("AGENT_CONFIG.type", "react")

        agent = cls(config=cm)
        agent._build_deep_agent()
        return agent

    def _build_deep_agent(self) -> None:
        """从 ConfigManager 解析配置并构造 DeepAgent。"""
        # ⚠️ 必须先 silence 再 import jiuwen 模块，否则模块级注册日志已经打出
        from dataagent import _silence_jiuwen_logging
        _silence_jiuwen_logging()

        from openjiuwen.harness import create_deep_agent

        self._detach_a2a_agents()

        model = build_model_from_config(self.config)

        system_prompt = build_system_prompt(self.config)

        workspace_path = self._resolve_workspace()
        self._checkpointer_spec = build_checkpointer_spec(
            self.config,
            workspace_root=workspace_path,
        )

        agent_name = "dataagent"
        agent_config = self.config.get("AGENT_CONFIG", {}) if hasattr(self.config, "get") else {}
        if isinstance(agent_config, dict):
            agent_name = agent_config.get("name", "dataagent")

        adapter = DeepAgentAdapter(self.config)
        workspace = adapter.build_workspace(workspace_path)
        access_policy = adapter.build_access_policy(workspace_path)
        sys_operations = adapter.build_sys_operations(access_policy, agent_name=agent_name)
        tools = adapter.build_tools(
            sys_operations.primary,
            read_sys_operation=sys_operations.read_only,
            todo_workspace=str(workspace_path),
        )
        mcps = adapter.build_mcps()
        a2a_agents = adapter.build_a2a_agents()
        skill_binding = adapter.build_skill_rail(
            sys_operations.read_only,
            access_policy=access_policy,
        )
        hitl_rail = adapter.build_hitl_rail()
        context_processor_rail = adapter.build_context_processor_rail()
        rails = []
        if skill_binding is not None:
            rails.append(skill_binding.rail)
        if hitl_rail is not None:
            rails.append(hitl_rail)
        rails.append(context_processor_rail)
        logger.trace(
            f"Built {len(tools)} tools, {len(mcps)} MCP servers, and {len(a2a_agents)} A2A agents for DeepAgent"
        )

        max_iter = 15
        if isinstance(agent_config, dict):
            mi = agent_config.get("max_iter")
            if mi is not None:
                max_iter = int(mi)

        deep_agent = create_deep_agent(
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            mcps=mcps,
            rails=rails or None,
            workspace=workspace,
            max_iterations=max_iter,
            enable_task_planning=False,
            sys_operation=sys_operations.primary,
        )
        registered_a2a_agents = adapter.register_a2a_agents(a2a_agents, deep_agent)
        self._deep_agent = deep_agent
        self._a2a_agents = registered_a2a_agents
        self._skill_binding = skill_binding
        logger.trace(f"DeepAgent built with workspace={workspace.root_path}")

    def _detach_a2a_agents(self) -> None:
        if not self._a2a_agents:
            return

        bindings = self._a2a_agents
        self._a2a_agents = []
        DeepAgentAdapter.unregister_a2a_agents(bindings, self._deep_agent)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(DeepAgentAdapter.stop_a2a_agents(bindings))
            return

        task = loop.create_task(DeepAgentAdapter.stop_a2a_agents(bindings))
        self._a2a_cleanup_tasks.add(task)
        task.add_done_callback(self._a2a_cleanup_tasks.discard)

    async def aclose(self) -> None:
        """Release registered A2A resources and stop started remote clients."""
        bindings = self._a2a_agents
        self._a2a_agents = []
        if bindings:
            DeepAgentAdapter.unregister_a2a_agents(bindings, self._deep_agent)
            await DeepAgentAdapter.stop_a2a_agents(bindings)
        if self._a2a_cleanup_tasks:
            await asyncio.gather(*self._a2a_cleanup_tasks, return_exceptions=True)
            self._a2a_cleanup_tasks.clear()

    async def refresh_skills(
        self,
        *,
        user_id: str | None = None,
        custom_dirs: list[str | Path] | tuple[str | Path, ...] | None = None,
    ) -> list[dict[str, str]]:
        """Refresh skills and optionally replace runtime user/custom directories."""
        if self._deep_agent is None:
            self._build_deep_agent()
        if self._skill_binding is None:
            return []

        skills = await self._skill_binding.refresh(user_id=user_id, custom_dirs=custom_dirs)
        return [
            {
                "name": skill.name,
                "description": skill.description,
                "path": str(skill.directory),
            }
            for skill in skills
        ]

    def _resolve_workspace(self) -> Path:
        """Resolve workspace directory from config or default."""
        ws_cfg = self.config.get("WORKSPACE", {}) if hasattr(self.config, "get") else {}
        if isinstance(ws_cfg, dict):
            ws_path = ws_cfg.get("path", "")
            if ws_path:
                if isinstance(ws_path, Sequence) and not isinstance(ws_path, (str, bytes)):
                    raise ValueError(
                        "WORKSPACE.path must be a single path; multiple workspace roots are not supported."
                    )
                p = Path(ws_path).expanduser().resolve()
                return p
        return Path.cwd() / ".dataagent_workspace"

    # ── 对话接口 ────────────────────────────────────────────────────────────

    async def chat(
        self,
        user_query: str,
        session_id: str | None = None,
        workspace: Path | str | None = None,
        initial_state: dict[str, Any] | None = None,
        checkpoint_id: str | None = None,
        human_feedback: Any = None,
        interrupt_id: str | None = None,
    ) -> dict[str, Any]:
        """单轮对话。

        Args:
            user_query: 用户问题。
            session_id: 会话 ID（可选，自动生成）。
            workspace: 工作目录覆盖（可选）。
            initial_state: 初始状态字典（可选）。
            checkpoint_id: 检查点 ID。当前兼容语义等同于 session_id。
            human_feedback: 用于恢复 HITL 的人工反馈。
            interrupt_id: 人工反馈对应的中断 ID。

        Returns:
            包含 ``messages`` 的 dict，最后一条消息为最终回答。
        """
        from openjiuwen.core.session.agent import create_agent_session

        if self._deep_agent is None:
            self._build_deep_agent()
        if self._deep_agent is None:
            return {"error": "DeepAgent not built", "final_answer": "Agent 构建失败"}

        sid = self._resolve_checkpoint_session_id(
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            initial_state=initial_state,
        )
        runtime_user_id = initial_state.get("user_id") if isinstance(initial_state, dict) else None
        if runtime_user_id is not None:
            await self.refresh_skills(user_id=str(runtime_user_id))

        async with checkpointer_lease(self._require_checkpointer_spec()):
            session = create_agent_session(
                session_id=sid,
                card=self._deep_agent.card,
            )
            self.session_id = sid

            try:
                # DeepAgent 要求 inputs 中包含 "query" 键
                resume_input = self._resolve_human_feedback(
                    human_feedback=human_feedback,
                    interrupt_id=interrupt_id,
                    initial_state=initial_state,
                )
                inputs: dict[str, Any] = {
                    "query": resume_input if resume_input is not None else user_query
                }
                if isinstance(initial_state, dict):
                    for k, v in initial_state.items():
                        if k not in inputs and k not in {"human_feedback", "interrupt_id"}:
                            inputs[k] = v

                await session.pre_run(inputs=inputs)
                result = await self._deep_agent.invoke(inputs, session)
                await session.post_run()
                self._session = session
                return self._normalize_result(
                    result,
                    user_query,
                    session_id=sid,
                )

            except Exception as e:
                logger.error(f"Chat failed: {e}")
                return {
                    "error": str(e),
                    "final_answer": f"抱歉，处理您的请求时出现错误：{str(e)}",
                    "session_id": sid,
                    "checkpoint_id": sid,
                }

    def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator:
        """流式对话。

        兼容旧的 ``astream(initial_state=..., stream_mode=...)`` 调用方式。
        产出 ``(mode, data)`` 元组以兼容 CLI 和 REST 层。
        """
        initial_state = kwargs.pop("initial_state", None)
        if args and isinstance(args[0], dict) and initial_state is None:
            initial_state = args[0]
        if not isinstance(initial_state, dict):
            initial_state = {}

        user_query = str(initial_state.get("user_query", "") or "")
        session_id = kwargs.pop("session_id", None)
        checkpoint_id = kwargs.pop("checkpoint_id", None)
        human_feedback = kwargs.pop("human_feedback", None)
        interrupt_id = kwargs.pop("interrupt_id", None)
        sid = self._resolve_checkpoint_session_id(
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            initial_state=initial_state,
        )

        resume_input = self._resolve_human_feedback(
            human_feedback=human_feedback,
            interrupt_id=interrupt_id,
            initial_state=initial_state,
        )

        return self._astream_impl(
            user_query=user_query,
            session_id=sid,
            initial_state=initial_state,
            resume_input=resume_input,
        )

    async def _astream_impl(
        self,
        user_query: str,
        session_id: str,
        initial_state: dict[str, Any],
        resume_input: Any = None,
    ) -> AsyncGenerator:
        """Internal streaming implementation."""
        from openjiuwen.core.session.agent import create_agent_session

        if self._deep_agent is None:
            self._build_deep_agent()
        if self._deep_agent is None:
            yield ("updates", {"error": "DeepAgent not built"})
            return

        runtime_user_id = initial_state.get("user_id")
        if runtime_user_id is not None:
            await self.refresh_skills(user_id=str(runtime_user_id))

        async with checkpointer_lease(self._require_checkpointer_spec()):
            session = create_agent_session(
                session_id=session_id,
                card=self._deep_agent.card,
            )
            self.session_id = session_id

            try:
                inputs: dict[str, Any] = {
                    "query": resume_input if resume_input is not None else user_query
                }
                for k, v in initial_state.items():
                    if k not in inputs and k not in {"human_feedback", "interrupt_id"}:
                        inputs[k] = v

                stream = self._deep_agent.stream(inputs, session)
                collected_messages: list[Any] = []
                collected_interrupts: list[dict[str, Any]] = []
                streamed_output_parts: list[str] = []
                final_output = ""
                has_llm_output = False

                async for chunk in stream:
                    # DeepAgent yields structured output from ReActAgent
                    # Wrap as (mode, data) tuples for backward compatibility
                    if isinstance(chunk, tuple):
                        yield chunk
                    else:
                        plain_chunk = self._to_plain_data(chunk)
                        interrupt = self._extract_interrupt(plain_chunk)
                        if interrupt is not None:
                            collected_interrupts.append(interrupt)
                            yield ("custom", {"type": "interaction", **interrupt})
                        else:
                            chunk_type = str(plain_chunk.get("type", "")) if isinstance(plain_chunk, Mapping) else ""
                            content = self._extract_stream_content(plain_chunk)
                            if chunk_type == "llm_usage":
                                continue
                            if chunk_type == "tracer_agent":
                                tool_event = self._extract_tool_stream_event(plain_chunk)
                                if tool_event is not None:
                                    yield ("custom", tool_event)
                                continue
                            if chunk_type == "llm_reasoning":
                                if content:
                                    yield ("custom", plain_chunk)
                                continue
                            if chunk_type == "answer":
                                final_output = content
                                if not has_llm_output and content:
                                    streamed_output_parts.append(content)
                                    yield ("custom", plain_chunk)
                                continue
                            if chunk_type == "llm_output":
                                has_llm_output = True
                                streamed_output_parts.append(content)
                                yield ("custom", plain_chunk)
                                continue
                            elif isinstance(plain_chunk, Mapping) and plain_chunk.get("type") and plain_chunk.get("payload"):
                                collected_messages.append({"content": content})
                            else:
                                collected_messages.append(plain_chunk)
                            yield ("custom", plain_chunk if isinstance(plain_chunk, dict) else {"type": "message", "content": content})

                self._session = session
                if collected_interrupts:
                    yield (
                        "updates",
                        {
                            "result_type": "interrupt",
                            "interrupted": True,
                            "interrupts": collected_interrupts,
                            "interrupt_ids": [
                                interrupt["interrupt_id"] for interrupt in collected_interrupts
                            ],
                            "complete": False,
                            "session_id": session_id,
                            "checkpoint_id": session_id,
                        },
                    )
                    return
                final_content = final_output or "".join(streamed_output_parts)
                if final_content:
                    collected_messages = [{"role": "assistant", "content": final_content}]
                update_payload = {
                    "messages": collected_messages,
                    "complete": True,
                    "session_id": session_id,
                    "checkpoint_id": session_id,
                }
                if final_content:
                    update_payload["final_answer"] = final_content
                yield (
                    "updates",
                    update_payload,
                )

            except Exception as e:
                logger.error(f"Stream failed: {e}")
                yield (
                    "updates",
                    {
                        "error": str(e),
                        "session_id": session_id,
                        "checkpoint_id": session_id,
                    },
                )

    # ── 信息接口 ────────────────────────────────────────────────────────────

    def get_agent_info(self) -> dict[str, Any]:
        """获取 Agent 信息。"""
        agent_config = self.config.get("AGENT_CONFIG", {}) if hasattr(self.config, "get") else {}
        return {
            "name": agent_config.get("name", "DataAgent") if isinstance(agent_config, dict) else "DataAgent",
            "version": agent_config.get("version", "1.0") if isinstance(agent_config, dict) else "1.0",
            "description": agent_config.get("description", "数据分析Agent")
            if isinstance(agent_config, dict)
            else "数据分析Agent",
            "backend": self.backend,
            "type": self.type,
        }

    def name(self) -> str:
        return str(self.get_agent_info().get("name", ""))

    def description(self) -> str:
        return str(self.get_agent_info().get("description", ""))

    def version(self) -> str:
        return str(self.get_agent_info().get("version", "1.0"))

    def update_config(self, new_config: dict[str, Any]) -> None:
        """热更新配置（触发 DeepAgent 重建）。"""
        if hasattr(self.config, "update"):
            self.config.update(new_config)
        self._build_deep_agent()
        logger.debug("DataAgent configuration updated, DeepAgent rebuilt")

    def build_agent_graph(self, mode: str = "chat") -> DeepAgent:
        """返回底层 DeepAgent 实例（兼容旧接口）。"""
        if mode != "chat":
            raise ValueError(f"Unsupported mode: {mode!r}")
        if self._deep_agent is None:
            self._build_deep_agent()
        assert self._deep_agent is not None
        return self._deep_agent

    # ── 内部辅助 ────────────────────────────────────────────────────────────

    def _resolve_session_id(
        self,
        session_id: str | None,
        initial_state: dict[str, Any] | None,
    ) -> str:
        """Resolve session ID from args / initial_state / existing / new."""
        if session_id:
            return str(session_id)
        if isinstance(initial_state, dict):
            sid = initial_state.get("session_id")
            if sid and str(sid).strip():
                return str(sid).strip()
        if self.session_id:
            return self.session_id
        new_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_") + str(uuid.uuid4())
        self.session_id = new_id
        return new_id

    def _resolve_checkpoint_session_id(
        self,
        *,
        session_id: str | None,
        checkpoint_id: str | None,
        initial_state: dict[str, Any] | None,
    ) -> str:
        """Resolve the OpenJiuWen session identity used as checkpoint alias."""
        normalized_session = str(session_id).strip() if session_id is not None else ""
        normalized_checkpoint = (
            str(checkpoint_id).strip() if checkpoint_id is not None else ""
        )
        if not normalized_checkpoint and isinstance(initial_state, dict):
            state_checkpoint = initial_state.get("checkpoint_id")
            if state_checkpoint is not None:
                normalized_checkpoint = str(state_checkpoint).strip()
        if (
            normalized_session
            and normalized_checkpoint
            and normalized_session != normalized_checkpoint
        ):
            raise ValueError(
                "checkpoint_id is a compatibility alias of session_id; "
                "both values must match when provided together"
            )
        return self._resolve_session_id(
            normalized_checkpoint or normalized_session or None,
            initial_state,
        )

    def _require_checkpointer_spec(self) -> CheckpointerSpec:
        if self._checkpointer_spec is None:
            self._checkpointer_spec = build_checkpointer_spec(
                self.config,
                workspace_root=self._resolve_workspace(),
            )
        return self._checkpointer_spec

    @staticmethod
    def _resolve_human_feedback(
        *,
        human_feedback: Any,
        interrupt_id: str | None,
        initial_state: dict[str, Any] | None,
    ) -> Any | None:
        if human_feedback is None and isinstance(initial_state, dict):
            human_feedback = initial_state.get("human_feedback")
        if interrupt_id is None and isinstance(initial_state, dict):
            state_interrupt_id = initial_state.get("interrupt_id")
            if state_interrupt_id is not None:
                interrupt_id = str(state_interrupt_id)
        if human_feedback is None:
            return None
        return build_interactive_input(human_feedback, interrupt_id=interrupt_id)

    @staticmethod
    def _normalize_result(
        result: dict[str, Any],
        user_query: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Normalize DeepAgent invoke result to DataAgent format."""
        if not isinstance(result, dict):
            return {
                "error": "unexpected result type",
                "final_answer": str(result),
                "session_id": session_id,
                "checkpoint_id": session_id,
            }

        if result.get("result_type") == "interrupt":
            plain_state = DataAgent._to_plain_data(result.get("state", []))
            interrupts = []
            if isinstance(plain_state, list):
                interrupts = [
                    interrupt
                    for item in plain_state
                    if (interrupt := DataAgent._extract_interrupt(item)) is not None
                ]
            raw_interrupt_ids = result.get("interrupt_ids")
            if not isinstance(raw_interrupt_ids, Sequence) or isinstance(
                raw_interrupt_ids, (str, bytes)
            ):
                raw_interrupt_ids = []
            interrupt_ids = [
                str(item)
                for item in raw_interrupt_ids
                if str(item).strip()
            ]
            return {
                "result_type": "interrupt",
                "interrupted": True,
                "interrupts": interrupts,
                "interrupt_ids": interrupt_ids,
                "state": plain_state,
                "complete": False,
                "user_query": user_query,
                "session_id": session_id,
                "checkpoint_id": session_id,
            }

        output = result.get("output") or result.get("content") or ""
        messages = result.get("messages", [])

        if not messages and output:
            messages = [{"role": "assistant", "content": str(output)}]

        return {
            "messages": messages,
            "final_answer": str(output) if output else "",
            "complete": True,
            "user_query": user_query,
            "session_id": session_id,
            "checkpoint_id": session_id,
        }

    @staticmethod
    def _to_plain_data(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return DataAgent._to_plain_data(value.model_dump(mode="json"))
        if isinstance(value, Mapping):
            return {str(key): DataAgent._to_plain_data(item) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [DataAgent._to_plain_data(item) for item in value]
        return value

    @staticmethod
    def _extract_interrupt(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping) or value.get("type") != "__interaction__":
            return None
        payload = value.get("payload", {})
        if not isinstance(payload, Mapping):
            return None
        interrupt_id = str(
            payload.get("id") or payload.get("interaction_id") or ""
        ).strip()
        request = payload.get("value", {})
        request = request if isinstance(request, Mapping) else {"value": request}
        return {
            "interrupt_id": interrupt_id,
            "interaction_type": "ask_user"
            if request.get("tool_name") == "ask_user" or request.get("questions")
            else "tool_confirmation",
            **DataAgent._to_plain_data(request),
        }

    @staticmethod
    def _extract_tool_status_event(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping) or value.get("type") != "tracer_agent":
            return None
        payload = value.get("payload", {})
        if not isinstance(payload, Mapping):
            return None
        metadata = payload.get("metaData", {})
        metadata = metadata if isinstance(metadata, Mapping) else {}
        if metadata.get("type") != "tool":
            return None

        status = str(payload.get("status", ""))
        normalized_status = "running" if status == "start" else status
        if status == "finish":
            normalized_status = "success"
        if payload.get("error"):
            normalized_status = "error"

        inputs = payload.get("inputs", {})
        tool_args = inputs.get("inputs", inputs) if isinstance(inputs, Mapping) else {}

        outputs = payload.get("outputs", {})
        output_payload: Any = {}
        if isinstance(outputs, Mapping):
            output_payload = outputs.get("outputs", outputs)
        tool_output = DataAgent._to_plain_data(output_payload)

        return {
            "type": "tool_status",
            "tool_call_id": str(payload.get("invokeId") or payload.get("traceId") or ""),
            "tool_name": str(payload.get("name") or metadata.get("class_name") or "unknown"),
            "status": normalized_status,
            "tool_args": DataAgent._to_plain_data(tool_args),
            "tool_output": tool_output,
            "content": DataAgent._extract_tool_content(tool_output),
            "error": str(payload.get("error") or ""),
        }

    @staticmethod
    def _extract_tool_stream_event(value: Any) -> dict[str, Any] | None:
        """Convert Jiuwen tracer tool chunks to Jiuwen CLI-style tool events."""
        status_event = DataAgent._extract_tool_status_event(value)
        if status_event is None:
            return None
        payload = {
            "tool_name": status_event.get("tool_name", "unknown"),
            "tool_args": status_event.get("tool_args", {}),
            "tool_call_id": status_event.get("tool_call_id", ""),
        }
        status = str(status_event.get("status") or "")
        if status in {"running", "start"}:
            return {"type": "tool_call", "payload": payload}
        payload["tool_result"] = status_event.get("content") or DataAgent._extract_tool_content(
            status_event.get("tool_output")
        )
        payload["tool_output"] = status_event.get("tool_output")
        payload["error"] = status_event.get("error", "")
        payload["status"] = status
        return {"type": "tool_result", "payload": payload}

    @staticmethod
    def _extract_tool_content(value: Any) -> str:
        if isinstance(value, Mapping):
            content = value.get("content")
            if content is not None:
                return str(content)
            data = value.get("data")
            if isinstance(data, Mapping):
                nested_content = data.get("content")
                if nested_content is not None:
                    return str(nested_content)
                skill_content = data.get("skill_content")
                if skill_content is not None:
                    return str(skill_content)
                filenames = data.get("filenames")
                if isinstance(filenames, Sequence) and not isinstance(filenames, (str, bytes, bytearray)):
                    return "\n".join(str(item) for item in filenames)
                files = data.get("files")
                dirs = data.get("dirs")
                if isinstance(files, Sequence) or isinstance(dirs, Sequence):
                    lines: list[str] = []
                    if isinstance(dirs, Sequence) and not isinstance(dirs, (str, bytes, bytearray)):
                        lines.extend(f"{item}/" for item in dirs)
                    if isinstance(files, Sequence) and not isinstance(files, (str, bytes, bytearray)):
                        lines.extend(str(item) for item in files)
                    return "\n".join(lines)
                if data:
                    return json.dumps(DataAgent._to_plain_data(data), ensure_ascii=False, indent=2)
            for key in ("output", "result", "message"):
                fallback = value.get(key)
                if fallback is not None:
                    return str(fallback)
            return ""
        return str(value) if value is not None else ""

    @staticmethod
    def _extract_stream_content(value: Any) -> str:
        if isinstance(value, Mapping):
            payload = value.get("payload")
            if isinstance(payload, Mapping):
                content = payload.get("content") or payload.get("output")
                if content is not None:
                    return str(content)
            content = value.get("content") or value.get("output")
            if content is not None:
                return str(content)
        return str(value)
