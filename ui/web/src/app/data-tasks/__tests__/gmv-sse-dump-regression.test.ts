import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  createInitialLiveRun,
  reduceLiveRunEvent,
} from "../live-run-state";
import { buildProcessToolGroups, deriveProcessGroupUsage } from "../process-tool-groups";
import { buildStepToolSummaries } from "../step-tool-summary";

const dumpDir = join(process.cwd(), "../../storage/tmp-gmv-repro");

function buildMessagesFromEvents(events: Array<Record<string, unknown>>) {
  const messages: Array<{
    id: string;
    role: string;
    toolCalls: Array<{ id: string; type: string; function: { name: string } }>;
    content: string;
  }> = [];
  const toolCallsByParent = new Map<string, Array<{ id: string; type: string; function: { name: string } }>>();

  for (const event of events) {
    if (event.type === "TEXT_MESSAGE_START") {
      messages.push({
        id: String(event.messageId),
        role: "assistant",
        toolCalls: [],
        content: "",
      });
    }
    if (event.type === "TEXT_MESSAGE_CONTENT") {
      const msg = messages.find((m) => m.id === event.messageId);
      if (msg) msg.content += String(event.delta ?? "");
    }
    if (event.type === "TOOL_CALL_START") {
      const parentId = String(event.parentMessageId ?? event.messageId ?? "");
      const list = toolCallsByParent.get(parentId) ?? [];
      list.push({
        id: String(event.toolCallId),
        type: "function",
        function: { name: String(event.toolCallName ?? event.toolName ?? "tool") },
      });
      toolCallsByParent.set(parentId, list);
    }
  }

  for (const message of messages) {
    const tools = toolCallsByParent.get(message.id);
    if (tools?.length) message.toolCalls = tools;
  }

  return messages.filter((m) => m.toolCalls.length > 0 || m.content.trim().length > 0);
}

describe("real GMV SSE dump regression", () => {
  it("reduces captured copilotkit events with zero residual running", () => {
    if (!existsSync(dumpDir)) return;
    const dumpFiles = readdirSync(dumpDir).filter((name) => name.endsWith(".json"));
    if (dumpFiles.length === 0) return;

    const dump = JSON.parse(
      readFileSync(join(dumpDir, dumpFiles.sort().at(-1)!), "utf8"),
    ) as { events: Array<Record<string, unknown>> };

    let liveRun = createInitialLiveRun();
    for (const event of dump.events) {
      liveRun = reduceLiveRunEvent(liveRun, event);
    }

    const messages = buildMessagesFromEvents(dump.events);
    const groups = buildProcessToolGroups(messages, liveRun);

    expect(liveRun.runStatus).toBe("completed");
    expect(liveRun.toolCalls.every((call) => call.status !== "running")).toBe(true);
    expect(deriveProcessGroupUsage(groups, liveRun).runningSteps).toBe(0);
    expect(groups.every((group) => group.status !== "running")).toBe(true);

    for (const message of messages) {
      if (message.role !== "assistant" || message.toolCalls.length === 0) continue;
      const summaries = buildStepToolSummaries({
        toolCalls: message.toolCalls,
        liveRun,
        isActive: false,
      });
      expect(summaries.every((tool) => tool.status !== "running")).toBe(true);
    }
  });
});
