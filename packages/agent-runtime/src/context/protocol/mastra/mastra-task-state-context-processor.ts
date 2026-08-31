import type {
  ProcessInputStepArgs,
  ProcessInputStepResult,
  Processor
} from "@mastra/core/processors";

import type { TaskStateRuntime } from "../../../memory/task-state-runtime.js";

type PersistedTask = {
  activeForm: string;
  content: string;
  id: string;
  status: "completed" | "in_progress" | "pending";
};

export type MastraTaskStateContextProcessorOptions = {
  runtime: TaskStateRuntime;
  threadId: string;
};

export class MastraTaskStateContextProcessor implements Processor<"task-state-context"> {
  readonly id = "task-state-context";
  readonly name = "Task State Context Processor";

  constructor(private readonly options: MastraTaskStateContextProcessorOptions) {}

  /** Inject the durable Mastra thread task snapshot before every model step. */
  async processInputStep(args: ProcessInputStepArgs): Promise<ProcessInputStepResult | undefined> {
    const store = await this.options.runtime.storage.getStore("threadState");
    const value = await store?.getState({ threadId: this.options.threadId, type: "task" });
    if (!Array.isArray(value) || !value.every(isPersistedTask) || value.length === 0) {
      return undefined;
    }

    const tasks = value.map((task) => {
      const label = task.status === "in_progress" ? task.activeForm : task.content;
      return `- [${task.status}] {id: ${escapeXml(task.id)}} ${escapeXml(label)}`;
    });
    return {
      messages: args.messages,
      systemMessages: [
        ...args.systemMessages,
        {
          role: "system",
          content: `<current-task-list>\n${tasks.join("\n")}\n</current-task-list>`
        }
      ]
    };
  }
}

const isPersistedTask = (value: unknown): value is PersistedTask => {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const task = value as Record<string, unknown>;
  return (
    typeof task.id === "string" &&
    typeof task.content === "string" &&
    typeof task.activeForm === "string" &&
    (task.status === "pending" || task.status === "in_progress" || task.status === "completed")
  );
};

const escapeXml = (value: string): string =>
  value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
