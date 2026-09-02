import { EventType, type BaseEvent } from "@ag-ui/client";

import {
  AGENT_INTERRUPT_TYPE,
  INTERRUPT_EVENT_NAME,
  RUNTIME_BOUND_EVENT,
  RUNTIME_CONTRACT_VERSION,
  RUNTIME_PROVIDER,
  type RuntimeRunRequest
} from "./types.js";

export type RuntimeStubScenario = "text" | "tool" | "interrupt";

export const resolveRuntimeStubScenario = (request: RuntimeRunRequest): RuntimeStubScenario => {
  if (request.resume) {
    return "text";
  }
  const lastUser = lastUserText(request);
  if (/\b(interrupt|ask)\b/i.test(lastUser)) {
    return "interrupt";
  }
  if (/\b(tool|plan)\b/i.test(lastUser)) {
    return "tool";
  }
  return "text";
};

export const buildRuntimeBoundEvent = (
  runId: string,
  checkpointRef = `ckpt:${runId}`
): BaseEvent => ({
  type: EventType.CUSTOM,
  name: RUNTIME_BOUND_EVENT,
  value: {
    provider: RUNTIME_PROVIDER,
    version: RUNTIME_CONTRACT_VERSION,
    checkpointRef
  },
  timestamp: Date.now()
} as BaseEvent);

export function* generateStubEvents(request: RuntimeRunRequest): Generator<BaseEvent> {
  const timestamp = Date.now();
  yield {
    type: EventType.RUN_STARTED,
    threadId: request.threadId,
    runId: request.runId,
    timestamp
  } as BaseEvent;
  yield buildRuntimeBoundEvent(request.runId);

  if (request.resume) {
    const interrupt = request.resume.interrupt;
    if (request.resume.response === false) {
      yield {
        type: EventType.RUN_FINISHED,
        threadId: request.threadId,
        runId: request.runId,
        status: "cancelled",
        timestamp: Date.now()
      } as BaseEvent;
      return;
    }
    yield {
      type: EventType.TOOL_CALL_RESULT,
      toolCallId: interrupt.toolCallId,
      toolCallName: interrupt.toolName,
      content: JSON.stringify(request.resume.response ?? {}),
      timestamp: Date.now()
    } as BaseEvent;
    yield* textReply(request, "已收到你的回复，继续。");
    yield {
      type: EventType.RUN_FINISHED,
      threadId: request.threadId,
      runId: request.runId,
      timestamp: Date.now()
    } as BaseEvent;
    return;
  }

  const scenario = resolveRuntimeStubScenario(request);
  if (scenario === "tool") {
    const toolCallId = `call_todo_${request.runId}`;
    yield {
      type: EventType.TOOL_CALL_START,
      toolCallId,
      toolCallName: "write_todos",
      timestamp: Date.now()
    } as BaseEvent;
    yield {
      type: EventType.TOOL_CALL_ARGS,
      toolCallId,
      delta: JSON.stringify({ todos: [{ content: "整理问题", status: "in_progress" }] }),
      timestamp: Date.now()
    } as BaseEvent;
    yield {
      type: EventType.TOOL_CALL_END,
      toolCallId,
      toolCallName: "write_todos",
      timestamp: Date.now()
    } as BaseEvent;
    yield {
      type: EventType.TOOL_CALL_RESULT,
      toolCallId,
      toolCallName: "write_todos",
      content: JSON.stringify({ ok: true }),
      timestamp: Date.now()
    } as BaseEvent;
    yield* textReply(request, "已记下待办，接下来用对话继续。");
    yield {
      type: EventType.RUN_FINISHED,
      threadId: request.threadId,
      runId: request.runId,
      timestamp: Date.now()
    } as BaseEvent;
    return;
  }

  if (scenario === "interrupt") {
    const toolCallId = `call_ask_${request.runId}`;
    yield {
      type: EventType.CUSTOM,
      name: INTERRUPT_EVENT_NAME,
      value: {
        type: AGENT_INTERRUPT_TYPE,
        toolCallId,
        toolName: "ask_user",
        runId: request.runId,
        args: {
          question: "需要我继续吗？",
          options: ["继续", "停止"]
        },
        suspendPayload: {
          question: "需要我继续吗？",
          options: ["继续", "停止"]
        },
        resumeSchema: { type: "object" }
      },
      timestamp: Date.now()
    } as BaseEvent;
    return;
  }

  yield* textReply(request, `这是 Deep Agents 接入桩的回复：${lastUserText(request) || "你好"}`);
  yield {
    type: EventType.RUN_FINISHED,
    threadId: request.threadId,
    runId: request.runId,
    timestamp: Date.now()
  } as BaseEvent;
}

function* textReply(request: RuntimeRunRequest, text: string): Generator<BaseEvent> {
  const messageId = `msg_${request.runId}`;
  yield {
    type: EventType.TEXT_MESSAGE_START,
    messageId,
    role: "assistant",
    timestamp: Date.now()
  } as BaseEvent;
  yield {
    type: EventType.TEXT_MESSAGE_CONTENT,
    messageId,
    delta: text,
    timestamp: Date.now()
  } as BaseEvent;
  yield {
    type: EventType.TEXT_MESSAGE_END,
    messageId,
    timestamp: Date.now()
  } as BaseEvent;
}

const lastUserText = (request: RuntimeRunRequest): string => {
  for (let index = request.messages.length - 1; index >= 0; index -= 1) {
    const message = request.messages[index];
    if (message?.role !== "user") {
      continue;
    }
    if (typeof message.content === "string") {
      return message.content;
    }
  }
  return "";
};
