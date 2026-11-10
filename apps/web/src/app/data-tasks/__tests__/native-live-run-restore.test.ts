import { describe, expect, it } from "vitest";
import {
  liveRunFromNativeMessages,
  mergeStoredLiveRun,
  type StoredLiveRunSnapshot,
} from "../conversation-restore";

describe("live run restore from checkpoint messages", () => {
  it("rebuilds tool args and results so the console can show them again", () => {
    const run = liveRunFromNativeMessages(
      [
        { id: "u", role: "user", content: "哪所学校免费午餐比例最高？" },
        {
          id: "a",
          role: "assistant",
          content: "",
          toolCalls: [
            {
              id: "call-1",
              function: {
                name: "task",
                arguments: JSON.stringify({ subagent_type: "nl2sql" }),
              },
            },
          ],
        },
        {
          id: "t",
          role: "tool",
          toolCallId: "call-1",
          content: JSON.stringify({ sql: "select 1", question: "免费午餐" }),
        },
        { id: "answer", role: "assistant", content: "Roosevelt Elementary" },
      ],
      "complete",
    );

    expect(run.runStatus).toBe("completed");
    expect(run.toolCalls).toHaveLength(1);
    expect(run.toolCalls[0]).toMatchObject({
      id: "call-1",
      name: "task",
      status: "success",
    });
    expect(run.toolCalls[0]?.result).toContain("select 1");
  });

  it("puts the subagent step list back onto the matching tool call", () => {
    const run = liveRunFromNativeMessages(
      [
        {
          id: "a",
          role: "assistant",
          toolCalls: [{ id: "call-1", function: { name: "task", arguments: "{}" } }],
        },
        { id: "t", role: "tool", toolCallId: "call-1", content: "done" },
      ],
      "complete",
    );
    const stored: StoredLiveRunSnapshot = {
      runStatus: "completed",
      runStartedAt: 1_000,
      runFinishedAt: 10_900,
      tools: [
        {
          id: "call-1",
          startedAtMs: 1_000,
          finishedAtMs: 10_900,
          progress: {
            agentName: "nl2sql",
            runs: [{ id: "child", name: "nl2sql", status: "completed" }],
            nextStepId: 1,
            updatedAtMs: 10_900,
            status: "completed",
            steps: [
              {
                id: "0",
                subagentRunId: "child",
                name: "generator",
                status: "completed",
                startedAtMs: 2_000,
                durationMs: 4_000,
              },
            ],
          },
        },
      ],
    };

    const merged = mergeStoredLiveRun(run, stored);
    expect(merged.runStartedAt).toBe(1_000);
    expect(merged.toolCalls[0]?.progress?.steps.map((step) => step.name)).toEqual(["generator"]);
    expect(merged.toolCalls[0]?.result).toBe("done");
  });
});
