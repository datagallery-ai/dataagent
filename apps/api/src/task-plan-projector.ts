import { EventType, type BaseEvent } from "@ag-ui/core";
import { createActivitySnapshot, type AgentRunContext } from "@datafoundry/agent-runtime";

type MastraTask = {
  activeForm: string;
  content: string;
  id: string;
  status: "completed" | "in_progress" | "pending";
};

const TASK_TOOL_NAMES = new Set(["task_write", "task_update", "task_complete", "task_check"]);

export class TaskPlanProjector {
  private readonly toolNamesByCallId = new Map<string, string>();
  private lastFingerprint = "";

  constructor(private readonly runContext: AgentRunContext) {}

  /** Convert Mastra task tool results into replayable AG-UI PLAN snapshots. */
  observe(event: BaseEvent): BaseEvent[] {
    if (
      event.type === EventType.TOOL_CALL_START &&
      typeof event.toolCallId === "string" &&
      typeof event.toolCallName === "string"
    ) {
      this.toolNamesByCallId.set(event.toolCallId, event.toolCallName);
      return [];
    }

    if (
      event.type !== EventType.TOOL_CALL_RESULT ||
      typeof event.toolCallId !== "string" ||
      typeof event.content !== "string"
    ) {
      return [];
    }

    const toolName = this.toolNamesByCallId.get(event.toolCallId);
    this.toolNamesByCallId.delete(event.toolCallId);
    if (!toolName || !TASK_TOOL_NAMES.has(toolName)) {
      return [];
    }

    const tasks = parseTasks(event.content);
    if (!tasks) {
      return [];
    }

    const fingerprint = JSON.stringify(tasks);
    if (fingerprint === this.lastFingerprint) {
      return [];
    }
    this.lastFingerprint = fingerprint;

    return [
      createActivitySnapshot(this.runContext, "PLAN", {
        source: "mastra-task-state",
        tasks: tasks.map((task) => ({
          active_form: task.activeForm,
          id: task.id,
          status: task.status === "in_progress" ? "running" : task.status,
          title: task.content
        }))
      })
    ];
  }
}

const parseTasks = (content: string): MastraTask[] | undefined => {
  try {
    const value: unknown = JSON.parse(content);
    if (!isRecord(value) || !Array.isArray(value.tasks) || !value.tasks.every(isMastraTask)) {
      return undefined;
    }
    return value.tasks;
  } catch {
    return undefined;
  }
};

const isMastraTask = (value: unknown): value is MastraTask =>
  isRecord(value) &&
  typeof value.id === "string" &&
  typeof value.content === "string" &&
  typeof value.activeForm === "string" &&
  (value.status === "pending" || value.status === "in_progress" || value.status === "completed");

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;
