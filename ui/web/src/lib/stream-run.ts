import type { Message, RunAgentInput } from "@ag-ui/core";

export const STREAM_RUNTIME_PATH = "/api/v1/runs";

/** Body accepted by `POST /api/v1/runs`: one new user text, no client-owned fields. */
export function toStreamRunInput(input: RunAgentInput): RunAgentInput {
  const user = [...(input.messages ?? [])].reverse().find((message) => message.role === "user");
  if (!user || typeof user.id !== "string" || user.id.length === 0) {
    throw new Error("A new user message is required");
  }
  const content = textContent(user);
  if (!content.trim()) {
    throw new Error("A nonempty text message is required");
  }
  return {
    threadId: input.threadId,
    runId: input.runId,
    messages: [{ id: user.id, role: "user", content }],
    tools: [],
    context: [],
    state: {},
    forwardedProps: {},
  };
}

/**
 * Events with no child attribution. Child lifecycle is rewritten before the
 * CopilotKit verifier: it drops unknown `SUBAGENT_*` types and treats step
 * names as unique across the whole run.
 */
export function isParentStreamEvent(value: unknown): boolean {
  if (!value || typeof value !== "object" || !("type" in value)) return false;
  const event = value as { type?: unknown; subagentRunId?: unknown };
  if (typeof event.type !== "string" || !event.type) return false;
  if (event.type.startsWith("SUBAGENT_")) return false;
  return typeof event.subagentRunId !== "string" || event.subagentRunId.length === 0;
}

export function formatStreamHttpError(status: number, body: string): string {
  let detail = body.trim();
  if (detail) {
    try {
      const parsed = JSON.parse(detail) as { error?: { message?: unknown } };
      const message = parsed.error?.message;
      if (typeof message === "string" && message.trim()) {
        detail = message.trim();
      }
    } catch {
      // Keep the raw body when it is not a JSON error envelope.
    }
  }
  return detail ? `HTTP ${status}: ${detail}` : `HTTP ${status}`;
}

export function takeSseFrames(buffer: string): { frames: string[]; rest: string } {
  const frames: string[] = [];
  let rest = buffer;
  while (true) {
    const match = /\r?\n\r?\n/.exec(rest);
    if (!match) break;
    frames.push(rest.slice(0, match.index));
    rest = rest.slice(match.index + match[0].length);
  }
  return { frames, rest };
}

/** JSON payload of one SSE frame, or undefined for comments and `[DONE]`. */
export function sseData(frame: string): string | undefined {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data || data === "[DONE]") return undefined;
  return data;
}

export type OpenChildStep = { rawStepName: string; subagentRunId: string };

export type ClientStreamEvent =
  | { parse: true; event: unknown }
  | { parse: false; event: Record<string, unknown> };

/**
 * Turn one SSE payload into events the installed AG-UI client can verify.
 * Child steps keep their original name on `rawStepName` and use a unique
 * `stepName` so they do not collide with the parent step of the same name.
 */
export function takeClientEvents(
  value: unknown,
  openSteps: Map<string, OpenChildStep>,
): ClientStreamEvent[] {
  const adapted = adaptStreamEvent(value);
  if (!adapted) return [];
  if (adapted.kind === "parent") {
    if (isRecord(value) && value.type === "RUN_FINISHED") {
      return [...closeOpenSteps(openSteps).map(forward), { parse: true, event: value }];
    }
    return [{ parse: true, event: value }];
  }
  const event = adapted.event;
  if (event.type === "STEP_STARTED" && typeof event.stepName === "string") {
    openSteps.set(event.stepName, {
      rawStepName: String(event.rawStepName ?? ""),
      subagentRunId: String(event.subagentRunId ?? ""),
    });
    return [forward(event)];
  }
  if (event.type === "STEP_FINISHED" && typeof event.stepName === "string") {
    openSteps.delete(event.stepName);
    return [forward(event)];
  }
  if (event.type === "CUSTOM" && isRecord(event.value)) {
    const innerType = event.value.type;
    if (innerType === "SUBAGENT_FINISHED" || innerType === "SUBAGENT_ERROR") {
      const owner = typeof event.value.subagentRunId === "string" ? event.value.subagentRunId : undefined;
      return [forward(event), ...closeOpenSteps(openSteps, owner).map(forward)];
    }
  }
  return [forward(event)];
}

function forward(event: Record<string, unknown>): ClientStreamEvent {
  return { parse: false, event };
}

function closeOpenSteps(
  openSteps: Map<string, OpenChildStep>,
  subagentRunId?: string,
): Record<string, unknown>[] {
  const events: Record<string, unknown>[] = [];
  for (const [stepName, step] of [...openSteps]) {
    if (subagentRunId && step.subagentRunId !== subagentRunId) continue;
    openSteps.delete(stepName);
    events.push({
      type: "STEP_FINISHED",
      stepName,
      subagentRunId: step.subagentRunId,
      rawStepName: step.rawStepName,
    });
  }
  return events;
}

function adaptStreamEvent(
  value: unknown,
): { kind: "parent" } | { kind: "forward"; event: Record<string, unknown> } | undefined {
  if (!isRecord(value) || typeof value.type !== "string" || !value.type) return undefined;
  const type = value.type;
  const childId = typeof value.subagentRunId === "string" ? value.subagentRunId : "";
  if (type.startsWith("SUBAGENT_")) {
    if (!childId) return undefined;
    return { kind: "forward", event: { type: "CUSTOM", name: "subagent", value: childLifecycle(value, type, childId) } };
  }
  if (childId) {
    if ((type !== "STEP_STARTED" && type !== "STEP_FINISHED") || typeof value.stepName !== "string" || !value.stepName) {
      return undefined;
    }
    return {
      kind: "forward",
      event: {
        type,
        stepName: `${childId}/${value.stepName}`,
        subagentRunId: childId,
        rawStepName: value.stepName,
      },
    };
  }
  return isParentStreamEvent(value) ? { kind: "parent" } : undefined;
}

function childLifecycle(value: Record<string, unknown>, type: string, childId: string): Record<string, unknown> {
  return {
    type,
    subagentRunId: childId,
    ...(typeof value.name === "string" ? { name: value.name } : {}),
    ...(typeof value.parentToolCallId === "string" ? { parentToolCallId: value.parentToolCallId } : {}),
    ...(typeof value.parentSubagentRunId === "string" ? { parentSubagentRunId: value.parentSubagentRunId } : {}),
    ...(typeof value.parentMessageId === "string" ? { parentMessageId: value.parentMessageId } : {}),
    ...(typeof value.message === "string" ? { message: value.message } : {}),
    ...(typeof value.code === "string" ? { code: value.code } : {}),
    ...(value.outcome !== undefined ? { outcome: value.outcome } : {}),
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

function textContent(message: Message): string {
  const content = message.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => {
      if (typeof part === "string") return part;
      if (
        part &&
        typeof part === "object" &&
        "type" in part &&
        part.type === "text" &&
        "text" in part &&
        typeof part.text === "string"
      ) {
        return part.text;
      }
      return "";
    })
    .join("");
}
