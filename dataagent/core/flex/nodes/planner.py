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
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage
from loguru import logger

from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.context.context import Context
from dataagent.core.flex.utils.context_from_state import get_context_for_flex_state
from dataagent.core.flex.utils.planner_prompt_builder import prepare_flex_planner_prompt
from dataagent.core.flex.workflow.state import FlexState
from dataagent.core.framework_adapters.runtime.context import get_stream_writer
from dataagent.core.managers.llm_manager.adapters import LLMResponse, normalize_usage_metadata
from dataagent.core.managers.llm_manager.llm_client import LLMCallError
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate
from dataagent.utils.compression_utils import infer_state_and_unpack_ir
from dataagent.utils.env_utils import get_env_bool
from dataagent.utils.formatting_utils import format_tool_calls_for_display
from dataagent.utils.messages_utils import parse_actions_to_ai_message, record_message


class Planner(BaseNode):
    """
    规划节点（Planner）。

    使用 LLM 进行推理并支持工具调用，与 Executor 配合完成 ReAct 工作流。
    """

    def __init__(
        self,
        name: str,
        chat_model: str | None = None,
        **kwargs,
    ):
        """
        初始化 Planner。

        Planner 始终加载内置基座模板。Flex YAML 的 ``prompt_template``
        会作为局部模板注入内置模板中的 Jinja 插槽，只追加不替换框架能力提示。
        """
        prompt_appends = kwargs.pop("prompt_appends", {}) or {}
        super().__init__(name=name, enabled=True, chat_model_name=chat_model, **kwargs)

        if chat_model is None:
            raise RuntimeError("Chat model name is required for planner.")
        self.llm = None

        ns = self.name or "planner"
        self.system_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/{ns}/system").with_partials(
            system_prompt_append=prompt_appends.get("system"),
        )
        self.user_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/{ns}/user").with_partials(
            user_prompt_append=prompt_appends.get("user"),
        )

    @staticmethod
    def _normalize_tool_calls_for_aimessage(raw: Any) -> list[dict[str, Any]]:
        """OpenAI 形态 ``{function:{name,arguments}}`` 转为 LangChain ``{name,args,id}``，避免构造 AIMessage 失败。"""
        if not raw:
            return []
        out: list[dict[str, Any]] = []
        for tc in raw:
            if not isinstance(tc, dict):
                continue
            if "name" in tc and "args" in tc:
                out.append(tc)
                continue
            fn = tc.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                tid = str(tc.get("id") or "")
                out.append({"id": tid, "name": str(fn["name"]), "args": args})
        return out

    @staticmethod
    def _to_ai_message(resp: Any) -> AIMessage:
        """将 LLM 返回值统一转为 AIMessage，保留 tool_calls / invalid_tool_calls。"""
        ts = time.time()
        if isinstance(resp, AIMessage):
            akw = getattr(resp, "additional_kwargs", None)
            if isinstance(akw, dict) and "_ts" not in akw:
                akw["_ts"] = ts
            return resp
        if isinstance(resp, LLMResponse):
            additional_kwargs: dict[str, Any] = {"_ts": ts}
            if resp.reasoning_content:
                additional_kwargs["reasoning_content"] = resp.reasoning_content
            return AIMessage(
                content=resp.content,
                tool_calls=Planner._normalize_tool_calls_for_aimessage(resp.tool_calls),
                invalid_tool_calls=resp.invalid_tool_calls,
                usage_metadata=normalize_usage_metadata(resp.usage_metadata),
                additional_kwargs=additional_kwargs,
            )
        return AIMessage(
            content=getattr(resp, "content", str(resp)),
            tool_calls=Planner._normalize_tool_calls_for_aimessage(getattr(resp, "tool_calls", [])),
            invalid_tool_calls=getattr(resp, "invalid_tool_calls", []),
            additional_kwargs={"_ts": ts},
        )

    async def _aprocess(self, state: FlexState, runtime: Any = None) -> dict[str, Any] | FlexState:
        """终端模式整段输出，前端模式流式输出。runtime 由 workflow._wrap_process 显式传入。"""
        if runtime is None:
            raise RuntimeError("Planner requires runtime; env.llm_configs must include this node")
        if self.llm is None:
            base = runtime.llm(self.name)
            tools = runtime.get_tools_for_llm()
            self.llm = base.bind_tools(tools) if tools else base

        # 意图未填满：仅由 hook 记录意图状态，此处作为正常 planner 结果返回缺口提示，
        # 不依赖 BaseNode 全局短路，避免影响 pruner 的消息归并与后处理节点执行。
        if not state.get("intent_complete", True) and state.get("missing_slots"):
            return {
                "messages": AIMessage(
                    content=state.get("intent_missing_message", "缺少必要信息，无法完成请求。"),
                    additional_kwargs={"_ts": time.time()},
                ),
                "complete": True,
                "intent_complete": False,
                "intent_slots": state.get("intent_slots", {}),
                "missing_slots": state.get("missing_slots", []),
                "intent_missing_message": state.get("intent_missing_message", ""),
            }

        writer = get_stream_writer()
        context = cast(Context, get_context_for_flex_state(state, runtime))
        unpacked_data_ir = ""
        current_state: dict[str, str] | None = None
        if runtime.get_config("CONTEXT.enable_profiling", False):
            context.profiling()
        await context.wait_pending_tasks()
        if runtime.get_config("CONTEXT.enable_state_inference", False):
            current_state, unpacked_data_ir = await infer_state_and_unpack_ir(context, runtime=runtime)
        messages_to_process = self._prepare_messages_to_process(state, context, runtime, unpacked_data_ir)
        _dump_context_prompt_if_enabled(messages_to_process, state, runtime)
        terminal_mode = bool(state.get("terminal_mode", False))
        streamed_content = False
        reasoning_emitted = False
        break_after_thinking_emitted = False
        final_resp: LLMResponse | None = None
        try:
            if terminal_mode:
                self._emit_planner_stream_event(writer, phase="start")
            async for chunk in self.llm.astream(messages_to_process):
                if chunk.done:
                    # 在最后一个 chunk 中可以获取到完整的输出
                    final_resp = chunk.final_response
                    continue
                if terminal_mode:
                    if chunk.reasoning_content:
                        self._emit_planner_stream_event(writer, phase="reasoning", content=chunk.reasoning_content)
                        reasoning_emitted = True
                    if chunk.content:
                        self._emit_planner_stream_event(writer, phase="content", content=chunk.content)
                        streamed_content = True
                    continue
                if chunk.reasoning_content:
                    # 必须用 reasoning_content，勿写入 content，否则前端会把 thinking 当正文与回复粘在一起
                    writer(
                        {
                            "type": "output_msg",
                            "node_name": self.name,
                            "content": "",
                            "reasoning_content": chunk.reasoning_content,
                        }
                    )
                    reasoning_emitted = True
                if not chunk.content:
                    continue
                # 思考流结束后、正文开始前插入 break，便于前端新开回答气泡
                if reasoning_emitted and not break_after_thinking_emitted:
                    writer({"type": "break"})
                    break_after_thinking_emitted = True
                if not streamed_content:
                    writer(
                        {
                            "type": "output_msg",
                            "node_name": self.name,
                            "content": f"\n\n**{self.name}:**\n\n{chunk.content}",
                        }
                    )
                    streamed_content = True
                else:
                    writer({"type": "output_msg", "node_name": self.name, "content": chunk.content})

            if final_resp is None:
                raise RuntimeError("llm.astream finished without final_response")

            ai_message = self._normalize_ai_message(final_resp)
            self._emit_ai_message(
                writer,
                ai_message,
                streamed_content=streamed_content,
                terminal_mode=terminal_mode,
                reasoning_already_emitted=reasoning_emitted,
            )
            return self._build_result(context, ai_message, state, current_state)

        except Exception as e:
            import traceback

            logger.error(f"Error traceback: {traceback.format_exc()}")
            return self._build_error_result(writer, e, terminal_mode=terminal_mode)

    def _prepare_messages_to_process(
        self, state: FlexState, context: Context, runtime: Any, unpacked_data_ir: str = ""
    ) -> Any:
        workspace = runtime.workspace_dir

        extra: dict[str, Any] = {}
        if state.get("enable_portrait"):
            memory_str = _build_memory_str(state, runtime=runtime)
            if memory_str:
                extra["memory"] = memory_str

        messages_to_process = prepare_flex_planner_prompt(
            context=context,
            state=state,
            system_prompt=self.system_prompt,
            user_prompt=self.user_prompt,
            runtime=runtime,
            workspace=workspace,
            **extra,
        )
        if unpacked_data_ir:
            messages_to_process += [HumanMessage(content=unpacked_data_ir)]
        return messages_to_process

    def _normalize_ai_message(self, resp: Any) -> AIMessage:
        ai_message = self._to_ai_message(resp)
        if not ai_message.tool_calls and not ai_message.invalid_tool_calls and ai_message.content:
            # preserve usage / reasoning when parse_actions_to_ai_message rebuilds AIMessage
            original_usage = normalize_usage_metadata(ai_message.usage_metadata)
            original_additional_kwargs = dict(ai_message.additional_kwargs or {})
            parsed_ai_message = parse_actions_to_ai_message(str(ai_message.content))
            merged_additional_kwargs = dict(parsed_ai_message.additional_kwargs or {})
            for key, value in original_additional_kwargs.items():
                merged_additional_kwargs.setdefault(key, value)
            ai_message = AIMessage(
                content=parsed_ai_message.content,
                tool_calls=parsed_ai_message.tool_calls,
                invalid_tool_calls=parsed_ai_message.invalid_tool_calls,
                usage_metadata=original_usage,
                additional_kwargs=merged_additional_kwargs,
            )
        return ai_message

    def _emit_ai_message(
        self,
        writer,
        ai_message: AIMessage,
        *,
        streamed_content: bool,
        terminal_mode: bool,
        reasoning_already_emitted: bool = False,
    ) -> None:
        reasoning = ai_message.additional_kwargs.get("reasoning_content", "") or ""

        # Trace-level logging for debugging
        if reasoning:
            logger.trace(f"[{self.name}] reasoning_content:\n{reasoning}")
        if ai_message.content:
            logger.trace(f"[{self.name}] content:\n{ai_message.content}")

        if terminal_mode and reasoning and not reasoning_already_emitted:
            self._emit_planner_stream_event(writer, phase="reasoning", content=reasoning)
            reasoning_already_emitted = True

        if ai_message.content:
            if terminal_mode:
                if not streamed_content:
                    self._emit_planner_stream_event(writer, phase="content", content=str(ai_message.content))
                    streamed_content = True
            elif streamed_content:
                # 流式阶段已在 output_msg 里拼过正文；此处仅收尾换行。
                # 若 reasoning 已随第一个 content chunk 提前发出则不再重复。
                tail: dict[str, Any] = {"type": "output_msg", "node_name": self.name, "content": "\n\n"}
                if reasoning and not reasoning_already_emitted:
                    tail["reasoning_content"] = reasoning
                writer(tail)
            else:
                writer(
                    {
                        "type": "output_msg",
                        "node_name": self.name,
                        "content": f"\n\n**{self.name}:**\n\n{ai_message.content}\n\n",
                        "reasoning_content": reasoning,
                    }
                )

        if ai_message.tool_calls:
            tool_info = format_tool_calls_for_display(ai_message.tool_calls)
            logger.trace(f"**正在调用以下工具:**\n\n{tool_info}\n\n")
            # 终端 CLI：通过结构化事件驱动 renderer；前端流式：保持旧 output_msg 契约
            if terminal_mode:
                self._emit_planner_tool_calls_event(writer, ai_message.tool_calls)
            else:
                tool_body = "\n".join([f"- **{tc['name']}**" for tc in ai_message.tool_calls])
                tool_event: dict[str, Any] = {
                    "type": "output_msg",
                    "node_name": self.name,
                    "content": f"**正在调用以下工具:**\n\n{tool_body}\n\n",
                    "tool_calls": ai_message.tool_calls,
                }
                # 只在 reasoning 尚未随 content/streaming chunk 发出时才附加，避免重复显示思考面板
                if reasoning and not reasoning_already_emitted:
                    tool_event["reasoning_content"] = reasoning
                    reasoning_already_emitted = True
                writer(tool_event)

        if terminal_mode:
            self._emit_planner_stream_event(writer, phase="end")

        writer({"type": "break"})

    def _emit_planner_stream_event(self, writer, *, phase: str, content: str = "") -> None:
        writer(
            {
                "type": "planner_stream",
                "node_name": self.name,
                "phase": phase,
                "content": content,
            }
        )

    def _emit_planner_tool_calls_event(self, writer, tool_calls: Sequence[Mapping[str, Any]]) -> None:
        writer(
            {
                "type": "planner_tool_calls",
                "node_name": self.name,
                "tool_calls": [dict(tool_call) for tool_call in tool_calls],
            }
        )

    def _emit_planner_error_event(self, writer, error_msg: str) -> None:
        writer(
            {
                "type": "planner_error",
                "node_name": self.name,
                "content": error_msg,
            }
        )

    def _build_result(
        self,
        context: Context,
        ai_message: AIMessage,
        state: FlexState,
        current_state: dict[str, str] | None = None,
    ) -> dict[str, Any] | FlexState:
        record_message(context, ai_message, perfect_state_space=current_state)

        curr_iter = int(state.get("curr_iter", 0)) + 1

        if self._has_hitl_request(ai_message):
            logger.trace(f"[{self.name}] 检测到 request_human_feedback 调用，设置 HITL 标志")
            return {
                "messages": ai_message,
                "need_human_feedback": True,
                "__hitl_in_current_turn__": False,
                "complete": False,
                "num_turns": 1,
                "curr_iter": curr_iter,
            }

        if len(ai_message.tool_calls) == 0 and len(ai_message.invalid_tool_calls) == 0:
            context.register_node(
                node_type="Response",
                description="agent response",
                predecessor_node=list(context.state.current_pt),
                response=ai_message.content,
                reasoning_content=ai_message.additional_kwargs.get("reasoning_content", ""),
            )
            return {
                "messages": ai_message,
                "complete": True,
                "num_turns": 1,
                "curr_iter": curr_iter,
            }

        return {
            "messages": ai_message,
            "complete": False,
            "num_turns": 1,
            "curr_iter": curr_iter,
        }

    def _build_error_result(self, writer, error: Exception, *, terminal_mode: bool = False) -> dict[str, Any]:
        if isinstance(error, LLMCallError):
            logger.error("❌ 推理执行错误: {}", error)
        else:
            logger.exception("❌ 推理执行错误: {}", error)
        # Hide internal error details from frontend responses.
        error_msg = "推理执行错误: 内部处理异常，请稍后重试"
        if terminal_mode:
            self._emit_planner_error_event(writer, error_msg)
        else:
            writer(
                {
                    "type": "output_msg",
                    "node_name": self.name,
                    "content": f"\n\n**{self.name} ❌ Error:**\n\n{error_msg}\n\n",
                }
            )
        writer({"type": "break"})
        error_ai_message = AIMessage(content=error_msg, additional_kwargs={"error": True})
        return {"messages": error_ai_message, "complete": True, "error": error_msg}

    def _has_hitl_request(self, ai_message: AIMessage) -> bool:
        """检测是否有 request_human_feedback 工具调用"""
        if not hasattr(ai_message, "tool_calls"):
            return False

        return any(tool_call.get("name") == "request_human_feedback" for tool_call in ai_message.tool_calls)


def _dump_context_prompt_if_enabled(messages_to_process: Any, state: FlexState, runtime: Any = None) -> None:
    """当 DATAAGENT_CONTEXT_DUMP 环境变量存在时，将当前轮 prompt 写入文件。"""
    if not get_env_bool("DATAAGENT_CONTEXT_DUMP"):
        return
    try:
        from dataagent.utils.messages_utils import dump_prompt_to_file
        from dataagent.utils.runtime_paths import resolve_flex_session_memory_dir

        config = runtime.get_all_config() if hasattr(runtime, "get_all_config") else None
        mem_dir = resolve_flex_session_memory_dir(
            user_id=str(state["user_id"]),
            session_id=str(state["session_id"]),
            workspace=state.get("workspace"),
            config=config,
        )
        dump_dir = mem_dir / "context_dump" / f"run_{state['run_id']}"
        dump_dir.mkdir(parents=True, exist_ok=True)
        curr_iter = int(state.get("curr_iter", 0))

        # 从 runtime.env 取实际压缩阈值，使 dump 的 approaching_compress 判断
        # 与 pruner / LLM 调用时的实际压缩阈值一致。
        compress_token_limit = None
        compress_message_cnt = None
        if runtime is not None:
            env = getattr(runtime, "env", None)
            if env is not None:
                compress_token_limit = getattr(env, "compress_token_limit", None)
                compress_message_cnt = getattr(env, "compress_message_cnt", None)

        annotate_bp = get_env_bool("DATAAGENT_CACHE_BREAKPOINT_ANNOTATION")
        dump_prompt_to_file(
            messages_to_process,
            dump_dir / f"round_{curr_iter}.txt",
            annotate_cache_breakpoints=annotate_bp,
            compress_token_limit=compress_token_limit,
            compress_message_cnt=compress_message_cnt,
        )

        logger.debug(f"Context dump saved to {dump_dir}")
    except Exception as e:
        logger.warning(f"Failed to dump context prompt: {e}")


def _build_memory_str(state: FlexState, *, runtime: Any = None) -> str:
    """读取 snapshot + profile + cross_session_memory，拼接为注入 prompt 的 memory 字符串。"""
    user_id = str(state.get("user_id") or "").strip()
    session_id = str(state.get("session_id") or "").strip()
    if not user_id or not session_id:
        return ""
    try:
        from dataagent.core.flex.hooks.history_writer import resolve_history_persistence_context
        from dataagent.core.flex.hooks.portraiter import _load_profile, _load_snapshot

        workspace, config = resolve_history_persistence_context(state, runtime)
        snapshot = _load_snapshot(
            user_id,
            session_id,
            workspace=workspace,
            config=config,
        )
        profile = _load_profile(user_id)
        parts: list[str] = []
        if any(v for v in snapshot.values()):
            snap_j = json.dumps(snapshot, ensure_ascii=False, indent=2)
            parts.append("**Session Snapshot:**\n```json\n" + snap_j + "\n```")
        if any(v for v in profile.values()):
            prof_j = json.dumps(profile, ensure_ascii=False, indent=2)
            parts.append("**User Profile:**\n```json\n" + prof_j + "\n```")

        # Cross-session memories
        cross_session_memory = str(state.get("cross_session_memory") or "").strip()
        if cross_session_memory:
            parts.append("**Cross-Session Memories:**\n" + cross_session_memory)

        return "\n\n".join(parts)
    except Exception:
        return ""
