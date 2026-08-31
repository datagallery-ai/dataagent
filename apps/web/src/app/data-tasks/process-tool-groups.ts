import { toolDisplayTitle } from "./data-task-state";
import type { LiveRun, LiveToolCallRecord } from "./live-run-state";

export type ProcessToolGroupStatus = LiveToolCallRecord["status"];

export type ProcessToolGroup = {
  id: string;
  messageId?: string;
  toolCallIds: string[];
  status: ProcessToolGroupStatus;
  startedAtMs?: number;
  finishedAtMs?: number;
  stepNumber: number;
  title: string;
  summary: string;
};

export type ProcessToolGroupUsage = {
  stepCount: number;
  completedSteps: number;
  failedSteps: number;
  runningSteps: number;
  toolCallCount: number;
};

type MessageLike = {
  id?: string;
  role?: string;
  toolCalls?: Array<{
    id?: string;
    function?: { name?: string };
  }>;
};

function toolCallStatusPriority(status: ProcessToolGroupStatus): number {
  if (status === "failed") return 3;
  if (status === "running") return 2;
  return 1;
}

function aggregateGroupStatus(calls: LiveToolCallRecord[]): ProcessToolGroupStatus {
  return calls.reduce<ProcessToolGroupStatus>(
    (status, call) =>
      toolCallStatusPriority(call.status) > toolCallStatusPriority(status)
        ? call.status
        : status,
    "success",
  );
}

function aggregateStartedAt(calls: LiveToolCallRecord[]): number | undefined {
  const starts = calls
    .map((call) => call.startedAtMs)
    .filter((value): value is number => value !== undefined);
  return starts.length > 0 ? Math.min(...starts) : undefined;
}

function aggregateFinishedAt(
  calls: LiveToolCallRecord[],
  status: ProcessToolGroupStatus,
): number | undefined {
  if (status === "running") return undefined;
  const finishes = calls
    .map((call) => call.finishedAtMs)
    .filter((value): value is number => value !== undefined);
  return finishes.length > 0 ? Math.max(...finishes) : undefined;
}

function groupTitle(calls: LiveToolCallRecord[]): string {
  if (calls.length === 0) return "Step";
  if (calls.length === 1) return toolDisplayTitle(calls[0]?.name);
  return `Run ${calls.length} tools in parallel`;
}

function groupSummary(calls: LiveToolCallRecord[]): string {
  const names = calls.map((call) => toolDisplayTitle(call.name));
  if (names.length === 0) return "No tool calls yet";
  const uniqueNames = [...new Set(names)];
  if (uniqueNames.length === 1 && names.length > 1) {
    return `${uniqueNames[0]} × ${names.length}`;
  }
  if (names.length <= 3) return names.join(", ");
  return `${names.slice(0, 3).join(", ")}, and ${names.length} tools`;
}

function createGroup(input: {
  id: string;
  messageId?: string;
  calls: LiveToolCallRecord[];
  stepNumber: number;
}): ProcessToolGroup {
  const status = aggregateGroupStatus(input.calls);
  return {
    id: input.id,
    messageId: input.messageId,
    toolCallIds: input.calls.map((call) => call.id),
    status,
    startedAtMs: aggregateStartedAt(input.calls),
    finishedAtMs: aggregateFinishedAt(input.calls, status),
    stepNumber: input.stepNumber,
    title: groupTitle(input.calls),
    summary: groupSummary(input.calls),
  };
}

export function buildProcessToolGroups(
  messages: MessageLike[],
  liveRun: LiveRun,
): ProcessToolGroup[] {
  const liveToolById = new Map(liveRun.toolCalls.map((call) => [call.id, call]));
  const assignedToolIds = new Set<string>();
  const groups: ProcessToolGroup[] = [];

  for (const message of messages) {
    if (message.role !== "assistant" || !Array.isArray(message.toolCalls)) {
      continue;
    }
    const calls = message.toolCalls
      .map((toolCall) => (toolCall.id ? liveToolById.get(toolCall.id) : undefined))
      .filter((call): call is LiveToolCallRecord => Boolean(call));
    if (calls.length === 0) continue;

    calls.forEach((call) => assignedToolIds.add(call.id));
    const fallbackId = calls.map((call) => call.id).join("-");
    groups.push(
      createGroup({
        id: `group-${message.id ?? fallbackId}`,
        ...(message.id ? { messageId: message.id } : {}),
        calls,
        stepNumber: groups.length + 1,
      }),
    );
  }

  for (const call of liveRun.toolCalls) {
    if (assignedToolIds.has(call.id)) continue;
    groups.push(
      createGroup({
        id: `group-${call.id}`,
        calls: [call],
        stepNumber: groups.length + 1,
      }),
    );
  }

  return groups;
}

export function deriveProcessGroupUsage(
  groups: ProcessToolGroup[],
  liveRun: LiveRun,
): ProcessToolGroupUsage {
  return {
    stepCount: groups.length,
    completedSteps: groups.filter((group) => group.status === "success").length,
    failedSteps: groups.filter((group) => group.status === "failed").length,
    runningSteps: groups.filter((group) => group.status === "running").length,
    toolCallCount: liveRun.toolCalls.length,
  };
}

export function processToolGroupsEqual(
  left: ProcessToolGroup[],
  right: ProcessToolGroup[],
): boolean {
  if (left.length !== right.length) return false;
  return left.every((group, index) => {
    const other = right[index];
    if (!other) return false;
    return (
      group.id === other.id &&
      group.messageId === other.messageId &&
      group.status === other.status &&
      group.startedAtMs === other.startedAtMs &&
      group.finishedAtMs === other.finishedAtMs &&
      group.stepNumber === other.stepNumber &&
      group.title === other.title &&
      group.summary === other.summary &&
      group.toolCallIds.length === other.toolCallIds.length &&
      group.toolCallIds.every((id, toolIndex) => id === other.toolCallIds[toolIndex])
    );
  });
}
