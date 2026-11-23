import { describe, expect, it } from "vitest";
import {
  buildCollapsedStepSummary,
  buildStepToolSummaries,
  buildToolChipSummaries,
  runningElapsedLabel,
  stepElapsedLabel,
  truncateThinkingPreview,
} from "../step-tool-summary";
import { createInitialLiveRun } from "../live-run-state";

describe("step-tool-summary", () => {
  it("shows thinking preview before tool summary when thinking exists", () => {
    const summary = buildCollapsedStepSummary({
      thinking: "我需要先确认 schema 和可用数据源，然后并行读取必要信息。",
      tools: [
        { id: "a", label: "查看数据源", status: "success", durationLabel: "120ms" },
        { id: "b", label: "检查 Schema", status: "success", durationLabel: "2.4s" },
      ],
    });

    expect(summary.thinkingPreview).toBe("我需要先确认 schema 和可用数据源，然后并行读取必要信息。");
    expect(summary.toolSummary).toBe("查看数据源 120ms · 检查 Schema 2.4s");
  });

  it("uses tool summary as primary collapsed body when thinking is absent", () => {
    const summary = buildCollapsedStepSummary({
      thinking: "",
      tools: [
        { id: "a", label: "查看数据源", status: "success", durationLabel: "120ms" },
        { id: "b", label: "检查 Schema", status: "running", durationLabel: "Running" },
        { id: "c", label: "生成查询", status: "failed", durationLabel: "Failed" },
      ],
    });

    expect(summary.thinkingPreview).toBeUndefined();
    expect(summary.toolSummary).toBe("3 tools in parallel · 查看数据源 120ms · 检查 Schema Running · 生成查询 Failed");
  });

  it("collapses extra chips into a +N item", () => {
    const chips = buildToolChipSummaries(
      [
        { id: "a", label: "A", status: "success", durationLabel: "1ms" },
        { id: "b", label: "B", status: "success", durationLabel: "2ms" },
        { id: "c", label: "C", status: "success", durationLabel: "3ms" },
        { id: "d", label: "D", status: "success", durationLabel: "4ms" },
      ],
      3,
    );

    expect(chips.map((chip) => chip.label)).toEqual(["A", "B", "+2"]);
  });

  it("formats step elapsed time from grouped timestamps", () => {
    expect(stepElapsedLabel({ startedAtMs: 10, finishedAtMs: 2450, status: "success" })).toBe("2.4s");
    expect(stepElapsedLabel({ startedAtMs: 10, status: "running" })).toBe("Running");
    expect(stepElapsedLabel({ status: "success" })).toBe("—");
  });

  it("keeps running tools labeled as Running in collapsed summaries", () => {
    const summary = buildCollapsedStepSummary({
      thinking: "I will inspect the schema first.",
      tools: [
        {
          id: "tc-schema",
          label: "Inspect Schema",
          status: "running",
          durationLabel: stepElapsedLabel({ status: "running", startedAtMs: 10 }),
        },
      ],
    });

    expect(summary.toolSummary).toBe("Inspect Schema Running");
  });

  it("truncates thinking preview to two compact lines", () => {
    expect(truncateThinkingPreview("第一行\n第二行\n第三行", 20)).toBe("第一行 第二行…");
  });

  it("prefers authoritative live run tool names over stale chat tool names", () => {
    const summaries = buildStepToolSummaries({
      toolCalls: [{ id: "call-ask-1", function: { name: "tool" } }],
      liveRun: {
        ...createInitialLiveRun(),
        toolCalls: [{ id: "call-ask-1", name: "ask_user", status: "running" }],
      },
      isActive: true,
    });

    expect(summaries[0]?.label).toBe("Ask user");
  });

  it("names the delegated subagent and the file a tool is touching", () => {
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        {
          id: "task",
          name: "task",
          status: "running" as const,
          startedAtMs: 1_000,
          progress: {
            agentName: "nl2sql",
            runs: [{ id: "child", name: "nl2sql", status: "running" as const }],
            nextStepId: 0,
            updatedAtMs: 0,
            status: "running" as const,
            steps: [],
          },
        },
        { id: "read", name: "read_file", status: "success" as const, startedAtMs: 1, finishedAtMs: 50 },
      ],
    };
    const summaries = buildStepToolSummaries({
      toolCalls: [
        { id: "task", function: { name: "task", arguments: "{\"subagent_type\":\"nl2sql\"}" } },
        { id: "read", function: { name: "read_file", arguments: "{\"file_path\":\"/workspace/notes.md\"}" } },
      ],
      liveRun,
      isActive: true,
      t: (key) => (key === "tools.task" ? "委托子代理" : key === "tools.read_file" ? "读取文件" : key),
    });
    expect(summaries.map((item) => item.label)).toEqual([
      "委托子代理 · nl2sql",
      "读取文件 · notes.md",
    ]);
    expect(summaries[0]?.startedAtMs).toBe(1_000);
  });

  it("keeps the end of a long command and switches long runs to minutes", () => {
    const command = `python analyze.py ${"x".repeat(80)}`;
    const summaries = buildStepToolSummaries({
      toolCalls: [{ id: "run", function: { name: "execute_command", arguments: JSON.stringify({ command }) } }],
      liveRun: {
        ...createInitialLiveRun(),
        toolCalls: [{ id: "run", name: "execute_command", status: "success", startedAtMs: 0, finishedAtMs: 90_000 }],
      },
      isActive: false,
      t: (key) => (key === "tools.execute_command" ? "运行命令" : key),
    });
    expect(summaries[0]?.label.startsWith("运行命令 · …")).toBe(true);
    expect(summaries[0]?.label.endsWith("x")).toBe(true);
    expect(summaries[0]?.label.length).toBeLessThanOrEqual("运行命令 · ".length + 48);
    expect(stepElapsedLabel({ status: "success", startedAtMs: 0, finishedAtMs: 90_000 })).toBe("1m 30s");
    expect(runningElapsedLabel(0, 400)).toBe("400ms");
    expect(runningElapsedLabel(0, 90_000)).toBe("1m 30s");
  });

  it("caps the collapsed chip row and shows every chip when asked", () => {
    const tools = ["A", "B", "C", "D"].map((label) => ({
      id: label,
      label,
      status: "success" as const,
      durationLabel: "1ms",
    }));
    expect(buildToolChipSummaries(tools).map((chip) => chip.label)).toEqual(["A", "B", "+2"]);
    expect(buildToolChipSummaries(tools, Number.POSITIVE_INFINITY).map((chip) => chip.label)).toEqual([
      "A",
      "B",
      "C",
      "D",
    ]);
  });
});
