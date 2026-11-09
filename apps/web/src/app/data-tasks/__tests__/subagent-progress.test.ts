import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createTranslator } from "../../../i18n/translate";
import { createInitialLiveRun, reduceLiveRunEvent } from "../live-run-state";
import { subagentProgressText, subagentSettledTrail, type SubagentProgress } from "../subagent-progress";
import { SubagentProgressLine } from "../subagent-progress-line";

vi.mock("../../../i18n/locale-context", () => ({
  useT: () => (key: string) => key,
}));

const childStart = (toolCallId: string, extra: Record<string, unknown> = {}) => ({
  type: "CUSTOM",
  name: "subagent",
  value: {
    type: "SUBAGENT_STARTED",
    subagentRunId: `child-${toolCallId}`,
    parentToolCallId: toolCallId,
    name: "nl2sql",
    ...extra,
  },
});

const step = (id: string, type = "STEP_STARTED", stepName = "generator") => ({
  type,
  subagentRunId: `child-${id}`,
  stepName: `child-${id}/${stepName}`,
  rawStepName: stepName,
});

describe("subagent progress", () => {
  it("attaches native child progress before a late tool start and keeps retries", () => {
    vi.spyOn(performance, "now").mockReturnValue(0);
    let state = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED", runId: "root" });
    state = reduceLiveRunEvent(state, childStart("task"));
    state = reduceLiveRunEvent(state, step("task"));
    expect(reduceLiveRunEvent(state, childStart("task"))).toBe(state);
    expect(reduceLiveRunEvent(state, step("task"))).toBe(state);
    state = reduceLiveRunEvent(state, {
      type: "TOOL_CALL_START",
      toolCallId: "task",
      toolCallName: "task",
    });
    vi.spyOn(performance, "now").mockReturnValue(42);
    state = reduceLiveRunEvent(state, step("task", "STEP_FINISHED"));
    state = reduceLiveRunEvent(state, step("task"));
    const progress = state.toolCalls[0]?.progress;
    expect(state.toolCalls).toHaveLength(1);
    expect(state.toolCalls[0]?.name).toBe("task");
    expect(progress?.steps.map((item) => item.status)).toEqual(["completed", "running"]);
    expect(progress?.steps[0]?.name).toBe("generator");
    expect(progress?.steps[0]?.durationMs).toBe(42);
    state = reduceLiveRunEvent(state, {
      type: "CUSTOM",
      name: "subagent",
      value: { type: "SUBAGENT_FINISHED", subagentRunId: "child-task" },
    });
    expect(state.toolCalls[0]?.status).toBe("running");
    expect(state.runStatus).toBe("running");
    state = reduceLiveRunEvent(state, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "task",
      content: JSON.stringify({ sql: "SELECT 1", error: null, row_count: 6, executed: true }),
    });
    expect(state.toolCalls[0]?.status).toBe("success");
    expect(reduceLiveRunEvent(state, step("task"))).toBe(state);
    vi.restoreAllMocks();
  });

  it("keeps a nested child on the parent tool and does not finish the root run", () => {
    let state = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED", runId: "root" });
    state = reduceLiveRunEvent(state, childStart("task"));
    state = reduceLiveRunEvent(state, step("task"));
    state = reduceLiveRunEvent(state, childStart("nested", { parentSubagentRunId: "child-task", parentToolCallId: undefined, name: "checker" }));
    expect(state.toolCalls).toHaveLength(1);
    expect(state.toolCalls[0]?.progress?.runs.map((run) => run.name)).toEqual(["nl2sql", "checker"]);
    state = reduceLiveRunEvent(state, {
      type: "TOOL_CALL_START",
      toolCallId: "inner",
      toolCallName: "read_file",
      subagentRunId: "child-nested",
    });
    expect(state.toolCalls).toHaveLength(1);
    state = reduceLiveRunEvent(state, {
      type: "CUSTOM",
      name: "subagent",
      value: { type: "SUBAGENT_ERROR", subagentRunId: "child-nested", message: "bad", code: "QueryError" },
    });
    expect(state.runStatus).toBe("running");
    expect(state.toolCalls[0]?.status).toBe("running");
    expect(state.toolCalls[0]?.progress?.runs.map((run) => run.status)).toEqual(["running", "failed"]);
    expect(state.toolCalls[0]?.progress?.runs[1]?.errorMessage).toBe("bad (QueryError)");
    expect(subagentProgressText(state.toolCalls[0]!.progress!, createTranslator("en"))).toContain(
      "checker: bad (QueryError)",
    );
    state = reduceLiveRunEvent(state, {
      type: "CUSTOM",
      name: "subagent",
      value: { type: "SUBAGENT_FINISHED", subagentRunId: "child-task" },
    });
    expect(state.toolCalls[0]?.progress?.status).toBe("failed");
    expect(state.runStatus).toBe("running");
  });

  it("closes open child steps when the root run fails", () => {
    let state = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED", runId: "root" });
    state = reduceLiveRunEvent(state, childStart("task"));
    state = reduceLiveRunEvent(state, step("task"));
    state = reduceLiveRunEvent(state, { type: "RUN_ERROR", message: "TimeoutError" });
    expect(state.toolCalls[0]?.status).toBe("failed");
    expect(state.toolCalls[0]?.progress?.steps[0]?.status).toBe("failed");
    expect(reduceLiveRunEvent(state, step("task"))).toBe(state);
  });

  it("does not mark a suspended child as successful", () => {
    let state = reduceLiveRunEvent(createInitialLiveRun(), { type: "RUN_STARTED", runId: "root" });
    state = reduceLiveRunEvent(state, childStart("task"));
    state = reduceLiveRunEvent(state, {
      type: "CUSTOM",
      name: "subagent",
      value: {
        type: "SUBAGENT_FINISHED",
        subagentRunId: "child-task",
        outcome: { type: "suspended", interruptIds: ["interrupt"] },
      },
    });
    expect(state.toolCalls[0]?.progress?.status).toBe("suspended");
    expect(state.toolCalls[0]?.status).toBe("running");
  });

  it("renders the current child step under the parent tool", () => {
    const progress: SubagentProgress = {
      agentName: "nl2sql",
      runs: [{ id: "child", name: "nl2sql", status: "running" }],
      nextStepId: 1,
      updatedAtMs: 0,
      status: "running",
      steps: [{ id: "0", subagentRunId: "child", name: "generator", status: "running", startedAtMs: 0 }],
    };
    const t = createTranslator("en");
    expect(subagentProgressText(progress, t)).toBe("nl2sql · generator");
    const html = renderToStaticMarkup(createElement(SubagentProgressLine, { progress }));
    expect(html).toContain("nl2sql · generator");
    expect(html).toMatch(/ · \d+s/);
  });

  it("keeps a settled step trail and does not say the result is still returning", () => {
    const progress: SubagentProgress = {
      agentName: "nl2sql",
      runs: [{ id: "child", name: "nl2sql", status: "completed" }],
      nextStepId: 3,
      updatedAtMs: 0,
      status: "completed",
      steps: [
        { id: "0", subagentRunId: "child", name: "generator", status: "completed", startedAtMs: 0 },
        { id: "1", subagentRunId: "child", name: "generator", status: "completed", startedAtMs: 1 },
        { id: "2", subagentRunId: "child", name: "validator", status: "completed", startedAtMs: 2 },
      ],
    };
    const t = createTranslator("zh-CN");
    expect(subagentProgressText(progress, t)).toContain("正在返回结果");
    expect(subagentSettledTrail(progress, t)).toBe("nl2sql · generator (2), validator");
    expect(subagentSettledTrail(progress, t)).not.toContain("正在返回结果");
  });

  it("keeps the child error on the settled line and names a nested agent", () => {
    const progress: SubagentProgress = {
      agentName: "nl2sql",
      runs: [
        { id: "child", name: "nl2sql", status: "failed" },
        { id: "nested", name: "checker", status: "failed", errorMessage: "Query failed" },
      ],
      nextStepId: 1,
      updatedAtMs: 0,
      status: "failed",
      steps: [{ id: "0", subagentRunId: "nested", name: "validator", status: "failed", startedAtMs: 0, durationMs: 10 }],
    };
    const t = createTranslator("zh-CN");
    expect(subagentSettledTrail(progress, t)).toBe("nl2sql · checker validator · 子代理出错 · Query failed");
  });
});
