"""Subclassable HITL handlers shared by native agent, model and tool stages.

Handlers describe business decisions. Middleware owns checkpointed preparation,
interrupts, answer collection and the original tool's execution.
"""

import asyncio
import inspect
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, TypedDict, get_type_hints
from uuid import uuid4

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import PrivateStateAttr, hook_config
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables.config import patch_config, set_config_context
from langchain_core.tools import tool
from langgraph._internal._constants import CONFIG_KEY_CALL, CONFIG_KEY_READ
from langgraph.channels.binop import BinaryOperatorAggregate
from langgraph.channels.delta import DeltaChannel
from langgraph.config import get_config
from langgraph.func import task
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from dataagent.config.declarations import HITLConfig, HITLPolicySpec, HITLStage
from dataagent.extensions.loading import PythonLoader


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TriggerDecision(_Model):
    triggered: bool = True
    reason: str = ""
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class InteractionRequest(_Model):
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list)

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value):
        if not value.strip():
            raise ValueError("Interaction question must not be blank")
        return value


class ResumeDecision(_Model):
    action: Literal["continue", "replan", "reject"] = "replan"
    message: str | None = None
    # Until a separate response handler is introduced, resume_handler may also
    # propose business updates. Only the middleware commits those updates.
    state_update: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("state_update")
    @classmethod
    def business_fields_only(cls, value):
        if {"messages", "hitl_history", "hitl_replans", "jump_to"} & value.keys():
            raise ValueError("HITL handlers cannot update framework state directly")
        return value


@dataclass(frozen=True)
class HITLContext:
    """Detached state and stage-specific data; runtime exposes borrowed host services."""

    state: dict[str, Any]
    runtime: Any
    tool_call: dict[str, Any] | None
    policy_id: str
    event: HITLStage = "before_tool"
    decision: TriggerDecision | None = None
    interaction: InteractionRequest | None = None
    answer: str | None = None
    tool_result: ToolMessage | Command | None = None


class HITLHandler:
    """Override only the required methods, with def or async def.

    Instances are shared across calls: keep interaction state in context/checkpoints,
    not self. Methods must be pure/idempotent because resume handlers can replay.
    """

    def trigger_policy(self, context: HITLContext, *, params: dict) -> TriggerDecision:
        """Every matching invocation triggers unless a subclass decides otherwise."""
        return TriggerDecision()

    def interaction_builder(self, context: HITLContext, *, params: dict) -> InteractionRequest:
        """Use a configured question/options, falling back to a generic question."""
        return InteractionRequest(
            question=params.get("question", "请补充需要确认的信息。"),
            options=params.get("options", []),
        )

    def resume_handler(self, context: HITLContext, *, params: dict) -> ResumeDecision:
        """Feed the answer back to the model without silently changing business state."""
        return ResumeDecision(
            message=f"用户反馈：{context.answer}\n请结合反馈重新推理，再决定后续操作。",
        )


@dataclass(frozen=True)
class HITLPolicy:
    spec: HITLPolicySpec
    handler: HITLHandler
    namespace: str = "main"

    @property
    def id(self):
        return f"{self.namespace}/{self.spec.id}"


def _merge_history(current, updates):
    return {**(current or {}), **updates}


class HITLState(AgentState):
    hitl_history: NotRequired[Annotated[dict[str, Any], PrivateStateAttr, _merge_history]]
    hitl_replans: NotRequired[Annotated[dict[str, bool], PrivateStateAttr, _merge_history]]


def _json_data(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _validate_handler(handler):
    if not isinstance(handler, HITLHandler):
        raise TypeError("HITL handler must inherit HITLHandler")
    for name in ("trigger_policy", "interaction_builder", "resume_handler"):
        method = getattr(handler, name)
        parameters = list(inspect.signature(method).parameters.values()) if callable(method) else []
        if (
            not callable(method)
            or inspect.isgeneratorfunction(method)
            or inspect.isasyncgenfunction(method)
            or len(parameters) != 2
            or parameters[0].kind
            not in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
            or parameters[1].name != "params"
            or parameters[1].kind != inspect.Parameter.KEYWORD_ONLY
        ):
            raise TypeError(f"HITL {name} must have signature (context, *, params)")


async def _invoke(handler, name, context, params, expected):
    method = getattr(handler, name)
    if inspect.iscoroutinefunction(method):
        result = await method(context, params=deepcopy(params))
    else:
        result = await asyncio.to_thread(method, context, params=deepcopy(params))
    if not isinstance(result, expected):
        if inspect.iscoroutine(result):
            result.close()
        raise TypeError(f"HITL {name} must return {expected.__name__}")
    return result


@dataclass
class _Outcome:
    updates: list[dict]
    records: dict
    feedback: list[HumanMessage]
    action: str = "continue"


async def _durable(func):
    # In the pinned LangGraph release, streaming a functional task can lose its
    # RETURN write. Invoke the task itself normally; model/tool callbacks still
    # stream events to the parent event consumer.
    config = get_config()
    call = config["configurable"][CONFIG_KEY_CALL]
    if isinstance(call, partial) and (call.keywords or {}).get("stream"):
        patched = partial(call.func, *call.args, **{**call.keywords, "stream": False})
        config = patch_config(
            config, configurable={**config["configurable"], CONFIG_KEY_CALL: patched}
        )

        async def invoke():
            return await func()

        with set_config_context(config) as context:
            return await asyncio.create_task(invoke(), context=context)
    return await func()


def _channels():
    # Same native local_read binding used by ToolNode to obtain state channels.
    read = get_config()["configurable"][CONFIG_KEY_READ]
    return read.args[1]


def _writes(updates):
    """Keep every reducer delta; collapse replacement channels to their last write."""
    channels = _channels()
    pairs = [(key, deepcopy(value)) for update in updates for key, value in update.items()]
    last = {key: index for index, (key, _) in enumerate(pairs)}
    return [
        (key, value)
        for index, (key, value) in enumerate(pairs)
        if isinstance(channels.get(key), (BinaryOperatorAggregate, DeltaChannel))
        or index == last[key]
    ]


def _preview(state, updates):
    result = deepcopy(dict(state))
    channels = _channels()
    working = {}
    for key, value in _writes(updates):
        if key not in channels:
            # Native graph updates ignore fields outside their output schema.
            continue
        if key not in working:
            channel = channels[key].copy()
            if key in state:
                if isinstance(channel, DeltaChannel):
                    from langgraph.types import Overwrite

                    channel.update([Overwrite(deepcopy(state[key]))])
                else:
                    channel = channel.from_checkpoint(deepcopy(state[key]))
            working[key] = channel
        working[key].update([value])
        result[key] = working[key].get()
    return result


def _tool_update(result, *, required=True):
    if isinstance(result, ToolMessage):
        return {"messages": [result]}
    if isinstance(result, Command) and result.graph is None and isinstance(result.update, dict):
        return result.update
    if required:
        raise TypeError("Approved tools must return ToolMessage or a local Command(update=dict)")
    return {}


class HITLMiddleware(AgentMiddleware):
    """Run one common HITL lifecycle at six stages, using ainvoke/astream."""

    state_schema = HITLState

    def __init__(self, policies: list[HITLPolicy], *, model_feedback=False):
        self.model_feedback = model_feedback
        self.tools = []
        if model_feedback:

            @tool
            async def request_human_feedback(
                question: str,
                runtime: ToolRuntime,
                reason: str = "",
                options: list[str] | None = None,
            ) -> ToolMessage:
                """Ask the user a blocking clarification or confirmation and wait for their reply."""
                if not question.strip():
                    raise ValueError("Feedback question must not be blank")
                validation_error = None
                while True:
                    answer = interrupt(
                        {
                            "type": "human_feedback",
                            "at": "model_request",
                            "interaction_id": runtime.tool_call_id,
                            "tool_call_id": runtime.tool_call_id,
                            "message": question,
                            "reason": reason,
                            "options": options or [],
                            "validation_error": validation_error,
                        }
                    )
                    if isinstance(answer, str) and answer.strip():
                        return ToolMessage(
                            content=answer,
                            name="request_human_feedback",
                            tool_call_id=runtime.tool_call_id,
                        )
                    validation_error = "请输入非空文本回答。"

            self.tools = [request_human_feedback]
        self.policies = {policy.id: policy for policy in policies}
        if len(self.policies) != len(policies):
            raise ValueError("HITL policy IDs must be unique")
        for policy in policies:
            _validate_handler(policy.handler)
        annotations = get_type_hints(HITLState, include_extras=True)
        for policy in policies:
            schema = getattr(policy.handler, "state_schema", None)
            if schema is not None:
                for key, value in get_type_hints(schema, include_extras=True).items():
                    if key in annotations and annotations[key] != value:
                        raise ValueError(f"Conflicting HITL state field: {key}")
                    annotations[key] = value
        self.state_schema = TypedDict("ConfiguredHITLState", annotations)

    @classmethod
    def from_config(cls, config: HITLConfig | dict, *, root: Path, namespace="main", loader=None):
        """Compile a HITL section relative to its source, optionally sharing a plugin loader."""
        config = config if isinstance(config, HITLConfig) else HITLConfig.model_validate(config)
        if not config.enabled:
            return cls([])
        loader = loader or PythonLoader()
        policies = []
        for spec in config.policies:
            handler_type = loader.load(root, spec.handler) if spec.handler else HITLHandler
            if not inspect.isclass(handler_type) or not issubclass(handler_type, HITLHandler):
                raise TypeError("HITL handler must reference an HITLHandler subclass")
            policies.append(HITLPolicy(spec, handler_type(), namespace))
        return cls(policies, model_feedback=True)

    def wrap_tool_call(self, request, handler):
        raise TypeError("HITLMiddleware requires ainvoke/astream")

    def _pending_policy(self, pending):
        policy = self.policies.get(pending["policy_id"])
        if (
            policy is None
            or policy.spec.version != pending["policy_version"]
            or policy.spec.at != pending["at"]
        ):
            raise ValueError("Pending HITL policy is missing or has an incompatible version")
        return policy

    async def _interact(self, event, state, runtime, call=None, tool_result=None):
        """Prepare once, collect ordered answers and return data without committing state.

        event: 当前生命周期阶段，如 before_model、before_tool 或 after_tool。
        call: 当前工具调用（id/name/args）；agent/model 阶段为 None。
              同一次工具调用在 before_tool 和 after_tool 中对应同一个 call。
        """
        key = event
        if call is not None:
            key += "_" + sha256(call["id"].encode()).hexdigest()[:24]

        @task(name="hitl_prepare_" + key)
        async def prepare():
            pending = []
            for policy in self.policies.values():
                if policy.spec.at != event:
                    continue
                if policy.spec.matcher is not None and policy.spec.matcher != call["name"]:
                    continue
                context = HITLContext(
                    deepcopy(dict(state)),
                    runtime,
                    deepcopy(call),
                    policy.id,
                    event=event,
                    tool_result=deepcopy(tool_result),
                )
                decision = await _invoke(
                    policy.handler,
                    "trigger_policy",
                    context,
                    policy.spec.params,
                    TriggerDecision,
                )
                if not decision.triggered:
                    continue
                interaction = await _invoke(
                    policy.handler,
                    "interaction_builder",
                    replace(context, decision=decision),
                    policy.spec.params,
                    InteractionRequest,
                )
                pending.append(
                    _json_data(
                        {
                            "interaction_id": (
                                call["id"] if event == "before_tool" else event + ":" + uuid4().hex
                            )
                            + ":"
                            + policy.id,
                            "at": event,
                            "policy_id": policy.id,
                            "policy_version": policy.spec.version,
                            "params": policy.spec.params,
                            "tool_call": call,
                            "decision": decision.model_dump(mode="json"),
                            "interaction": interaction.model_dump(mode="json"),
                        }
                    )
                )
            return pending

        # Always replay preparation, including when policies were removed on rebuild.
        # Only JSON preparation results are checkpointed; requests/runtime are closures.
        outcome = _Outcome([], {}, [])
        for item in await _durable(prepare):
            policy = self._pending_policy(item)
            if item["tool_call"] != call:
                raise ValueError("Pending HITL interaction does not match the original tool call")
            interaction = InteractionRequest.model_validate(item["interaction"])
            validation_error = None
            while True:
                answer = interrupt(
                    {
                        "type": "human_feedback",
                        "at": event,
                        "interaction_id": item["interaction_id"],
                        "policy_id": policy.id,
                        "policy_version": item["policy_version"],
                        "tool_call_id": call["id"] if call else None,
                        "message": interaction.question,
                        "options": interaction.options,
                        "reason": item["decision"]["reason"],
                        "validation_error": validation_error,
                    }
                )
                if isinstance(answer, str) and answer.strip():
                    break
                validation_error = "请输入非空文本回答。"

            context = HITLContext(
                _preview(state, outcome.updates),
                runtime,
                deepcopy(call),
                policy.id,
                event=event,
                tool_result=deepcopy(tool_result),
                decision=TriggerDecision.model_validate(item["decision"]),
                interaction=interaction,
                answer=answer,
            )
            resume = await _invoke(
                policy.handler,
                "resume_handler",
                context,
                item["params"],
                ResumeDecision,
            )
            outcome.updates.append(_json_data(resume.state_update))
            outcome.records[item["interaction_id"]] = {
                **item,
                "answer": answer,
                "action": resume.action,
                "source": "external",
            }
            outcome.feedback.append(
                HumanMessage(
                    id="hitl_feedback_" + item["interaction_id"],
                    content=resume.message or f"{resume.action}: 用户反馈：{answer}",
                )
            )
            outcome.action = resume.action
            if resume.action != "continue":
                break
        return outcome

    async def _state_event(self, event, state, runtime):
        routes = {key: False for key, active in state.get("hitl_replans", {}).items() if active}
        # Native return_direct tools bypass the model. Re-enter it at the exit
        # hook; mixed batches already go to before_model and consume these flags there.
        if routes and event == "after_agent":
            return {"hitl_replans": routes, "jump_to": "model"}
        outcome = await self._interact(event, state, runtime)
        update = {"hitl_replans": routes} if routes and event == "before_model" else {}
        if not outcome.records:
            return update or None
        messages = []
        # After-model continuation must leave the planned AIMessage last so the
        # native router still sees its tool calls. Other continuations end or
        # already have a model call ahead of them.
        if outcome.action != "continue" or event.startswith("before_"):
            if event == "after_model" and isinstance(state["messages"][-1], AIMessage):
                messages.extend(
                    ToolMessage(
                        id="hitl_skipped_" + call["id"],
                        name=call["name"],
                        tool_call_id=call["id"],
                        status="error",
                        content=outcome.feedback[-1].content,
                    )
                    for call in state["messages"][-1].tool_calls
                )
            messages.extend(outcome.feedback)
        if outcome.action == "reject":
            messages.append(
                AIMessage(
                    id=outcome.feedback[-1].id + "_rejected",
                    content="本次执行已被拒绝。\n" + outcome.feedback[-1].content,
                )
            )
            # after_agent is already on the exit path; jumping to end there
            # would route back to after_agent in the native middleware graph.
            if event != "after_agent":
                update["jump_to"] = "end"
        elif outcome.action == "replan" and event.startswith("after_"):
            update["jump_to"] = "model"
        if outcome.action != "continue" and "structured_response" in state:
            update["structured_response"] = None
        if messages:
            update["messages"] = messages
        return Command(
            update=_writes([*outcome.updates, {"hitl_history": outcome.records}, update])
        )

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(self, state, runtime):
        return await self._state_event("before_agent", state, runtime)

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        return await self._state_event("before_model", state, runtime)

    @hook_config(can_jump_to=["model", "end"])
    async def aafter_model(self, state, runtime):
        if self.model_feedback and isinstance(state["messages"][-1], AIMessage):
            last = state["messages"][-1]
            feedback = [
                call for call in last.tool_calls if call["name"] == "request_human_feedback"
            ]
            if feedback:
                # Ask before doing any parallel work; discard other proposals
                # and let the model regenerate them after receiving the answer.
                return {"messages": [last.model_copy(update={"tool_calls": feedback[:1]})]}
        return await self._state_event("after_model", state, runtime)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state, runtime):
        return await self._state_event("after_agent", state, runtime)

    @staticmethod
    def _tool_command(request, updates, action, result=None):
        if action != "continue" and getattr(request.tool, "return_direct", False):
            updates = [*updates, {"hitl_replans": {request.tool_call["id"]: True}}]
        # ToolNode accepts a list of Commands with one terminating ToolMessage.
        # Emit reducer writes separately rather than collapsing or re-reducing them.
        pairs = _writes(updates)
        if len({key for key, _ in pairs}) == len(pairs):
            update = dict(pairs)
            return (
                replace(result, update=update)
                if isinstance(result, Command)
                else Command(update=update)
            )
        commands = [Command(update={key: value}) for key, value in pairs]
        if isinstance(result, Command):
            commands[-1] = replace(result, update=commands[-1].update)
        return commands

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] == "request_human_feedback" and self.model_feedback:
            return await handler(request)
        before = await self._interact("before_tool", request.state, request.runtime, call)
        if before.action != "continue":
            return self._tool_command(
                request,
                [
                    *before.updates,
                    {
                        "hitl_history": before.records,
                        "messages": [
                            ToolMessage(
                                id="hitl_skipped_" + call["id"],
                                name=call["name"],
                                tool_call_id=call["id"],
                                status="error",
                                content=before.feedback[-1].content,
                            )
                        ],
                    },
                ],
                before.action,
            )

        preview = _preview(request.state, before.updates)
        approved = replace(request, state=preview, runtime=replace(request.runtime, state=preview))
        key = sha256(call["id"].encode()).hexdigest()[:24]

        @task(name="hitl_execute_" + key)
        async def execute():
            return await handler(approved)

        result = await _durable(execute)
        tool_update = _tool_update(result, required=False)
        preview = _preview(preview, [tool_update])
        after = await self._interact("after_tool", preview, request.runtime, call, result)
        if not before.records and not after.records:
            return result
        _tool_update(result)  # Validate only outputs from tools actually gated by HITL.
        if after.action != "continue":
            tool_update = {
                **tool_update,
                "messages": [*tool_update.get("messages", []), *after.feedback],
            }
        updates = [
            *before.updates,
            tool_update,
            *after.updates,
            {
                "hitl_history": {**before.records, **after.records},
            },
        ]
        return self._tool_command(request, updates, after.action, result)
