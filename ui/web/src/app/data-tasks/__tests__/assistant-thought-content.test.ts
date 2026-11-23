import { describe, expect, it } from "vitest";
import {
  dedupeRepeatedText,
  hasMeaningfulText,
  isOrphanPreambleMergedIntoFollowingToolStep,
  mergeMessagesForStepContext,
  messageTextContent,
  reasoningMessageAbsorbedByFollowingToolStep,
  resolveAssistantThoughtContent,
  resolveToolStepThoughtContent,
} from "../assistant-thought-content";

describe("assistant-thought-content", () => {
  it("extracts text from string and text parts", () => {
    expect(messageTextContent(" hello ")).toBe("hello");
    expect(
      messageTextContent([
        { type: "text", text: "a" },
        { type: "text", text: "b" },
      ]),
    ).toBe("ab");
  });

  it("treats punctuation/whitespace-only text as not meaningful", () => {
    expect(hasMeaningfulText(".")).toBe(false);
    expect(hasMeaningfulText(" 。， \n ")).toBe(false);
    expect(hasMeaningfulText("是")).toBe(true);
    expect(hasMeaningfulText("ok")).toBe(true);
    expect(hasMeaningfulText("123")).toBe(true);
  });

  it("drops trivial '.' preamble so it does not render as thinking", () => {
    const messages = [
      { id: "user-1", role: "user", content: "question" },
      {
        id: "assistant-tools",
        role: "assistant",
        content: ".",
        toolCalls: [{ id: "tc-1", function: { name: "list_data_sources" } }],
      },
    ];

    expect(resolveToolStepThoughtContent(messages[1], messages)).toBe("");
  });

  it("drops a trivial text-only assistant turn from thought content", () => {
    const messages = [
      { id: "user-1", role: "user", content: "question" },
      { id: "assistant-trivial", role: "assistant", content: " 。 " },
    ];

    expect(resolveToolStepThoughtContent(messages[1], messages)).toBe("");
    expect(resolveAssistantThoughtContent(messages[1], messages)).toBe("");
  });

  it("extracts explicit reasoning parts from model reasoning messages", () => {
    expect(
      messageTextContent([
        { type: "reasoning", text: "inspect schema" },
        { type: "text", text: " then query" },
      ]),
    ).toBe("inspect schema then query");
  });

  it("uses folded reasoning parts as tool-step thinking without final text", () => {
    const thought = "先检查 schema，再汇总 GMV。";
    const messages = [
      { id: "user-1", role: "user", content: "分析 GMV" },
      {
        id: "assistant-1",
        role: "assistant",
        content: [
          { type: "reasoning", text: thought },
          { type: "text", text: "查询完成。" },
        ],
        toolCalls: [{ id: "tc-1", function: { name: "run_sql_readonly" } }],
      },
    ];

    expect(resolveToolStepThoughtContent(messages[1], messages)).toBe(thought);
    expect(resolveAssistantThoughtContent(messages[1], messages)).toBe(thought);
  });

  it("dedupes exact repeated assistant text", () => {
    const thought =
      "我需要分析不同品类的销售额数据。首先，让我查看可用的数据源，然后检查数据库模式以了解表结构。";
    expect(dedupeRepeatedText(`${thought}${thought}`)).toBe(thought);
  });

  it("prefers preceding reasoning messages over duplicated assistant text", () => {
    const thought = "Let me inspect the schema first.";
    const content = resolveAssistantThoughtContent(
      {
        id: "assistant-1",
        role: "assistant",
        content: `${thought}${thought}`,
      },
      [
        { id: "user-1", role: "user", content: "question" },
        { id: "reasoning-1", role: "reasoning", content: thought },
        {
          id: "assistant-1",
          role: "assistant",
          content: `${thought}${thought}`,
        },
      ],
    );

    expect(content).toBe(thought);
  });

  it("merges a preceding text-only assistant preamble into the following tool step", () => {
    const preamble = "我将并行Call list_data_sources 三次。";
    const messages = [
      { id: "user-1", role: "user", content: "同时执行三个list_data_sources" },
      { id: "assistant-preamble", role: "assistant", content: preamble },
      {
        id: "assistant-tools",
        role: "assistant",
        content: "",
        toolCalls: [
          { id: "tc-1", function: { name: "list_data_sources" } },
          { id: "tc-2", function: { name: "list_data_sources" } },
          { id: "tc-3", function: { name: "list_data_sources" } },
        ],
      },
    ];

    expect(
      isOrphanPreambleMergedIntoFollowingToolStep(messages[1], messages),
    ).toBe(true);
    expect(
      resolveToolStepThoughtContent(messages[2], messages),
    ).toBe(preamble);
  });

  it("keeps standalone thought turns when no tool step follows", () => {
    const messages = [
      { id: "user-1", role: "user", content: "question" },
      { id: "assistant-thought", role: "assistant", content: "just thinking" },
    ];

    expect(
      isOrphanPreambleMergedIntoFollowingToolStep(messages[1], messages),
    ).toBe(false);
  });

  it("merges reasoning messages from render props into agent messages", () => {
    const thought = "我将并行调用 list_data_sources 三次。";
    const agentMessages = [
      { id: "user-1", role: "user", content: "同时执行三个list_data_sources" },
      {
        id: "assistant-tools",
        role: "assistant",
        content: "",
        toolCalls: [
          { id: "tc-1", function: { name: "list_data_sources" } },
          { id: "tc-2", function: { name: "list_data_sources" } },
          { id: "tc-3", function: { name: "list_data_sources" } },
        ],
      },
    ];
    const renderMessages = [
      { id: "user-1", role: "user", content: "同时执行三个list_data_sources" },
      { id: "reasoning-1", role: "reasoning", content: thought },
      {
        id: "assistant-tools",
        role: "assistant",
        content: [{ type: "reasoning", text: thought }],
        toolCalls: [
          { id: "tc-1", function: { name: "list_data_sources" } },
          { id: "tc-2", function: { name: "list_data_sources" } },
          { id: "tc-3", function: { name: "list_data_sources" } },
        ],
      },
    ];

    const merged = mergeMessagesForStepContext(agentMessages, renderMessages);
    expect(
      resolveToolStepThoughtContent(
        merged.find((item) => item.id === "assistant-tools")!,
        merged,
      ),
    ).toBe(thought);
  });

  it("prefers richer inline reasoning content from render props", () => {
    const merged = mergeMessagesForStepContext(
      [
        {
          id: "assistant-tools",
          role: "assistant",
          content: "",
          toolCalls: [{ id: "tc-1", function: { name: "inspect_schema" } }],
        },
      ],
      [
        {
          id: "assistant-tools",
          role: "assistant",
          content: [{ type: "reasoning", text: "先检查 schema。" }],
          toolCalls: [{ id: "tc-1", function: { name: "inspect_schema" } }],
        },
      ],
    );

    expect(
      resolveToolStepThoughtContent(merged[0], merged),
    ).toBe("先检查 schema。");
  });

  it("resolves thought from merged timeline when the render prop message is stale", () => {
    const thought = "我将并行调用 list_data_sources 三次。";
    const staleMessage = {
      id: "assistant-tools",
      role: "assistant",
      content: "",
      toolCalls: [
        { id: "tc-1", function: { name: "list_data_sources" } },
        { id: "tc-2", function: { name: "list_data_sources" } },
        { id: "tc-3", function: { name: "list_data_sources" } },
      ],
    };
    const merged = mergeMessagesForStepContext(
      [staleMessage],
      [
        {
          ...staleMessage,
          content: [{ type: "reasoning", text: thought }],
        },
      ],
    );

    expect(resolveToolStepThoughtContent(staleMessage, merged)).toBe(thought);
  });

  it("collects reasoning messages that appear after a tool step message", () => {
    const thought = "我将并行调用 list_data_sources 三次。";
    const messages = [
      { id: "user-1", role: "user", content: "同时执行三个list_data_sources" },
      {
        id: "assistant-tools",
        role: "assistant",
        content: "",
        toolCalls: [
          { id: "tc-1", function: { name: "list_data_sources" } },
          { id: "tc-2", function: { name: "list_data_sources" } },
          { id: "tc-3", function: { name: "list_data_sources" } },
        ],
      },
      { id: "reasoning-1", role: "reasoning", content: thought },
    ];

    expect(
      resolveToolStepThoughtContent(messages[1], messages),
    ).toBe(thought);
  });

  it("collects reasoning behind a text-only preamble before multi-tool steps", () => {
    const thought = "先想清楚再并行调用。";
    const preamble = "我将并行调用 list_data_sources。";
    const messages = [
      { id: "user-1", role: "user", content: "question" },
      { id: "reasoning-1", role: "reasoning", content: thought },
      { id: "assistant-preamble", role: "assistant", content: preamble },
      {
        id: "assistant-tools",
        role: "assistant",
        content: "",
        toolCalls: [
          { id: "tc-1", function: { name: "list_data_sources" } },
          { id: "tc-2", function: { name: "list_data_sources" } },
        ],
      },
    ];

    expect(
      resolveToolStepThoughtContent(messages[3], messages),
    ).toBe(`${thought}\n\n${preamble}`);
  });

  it("hides reasoning bubbles that belong to the next tool step", () => {
    const messages = [
      { id: "user-1", role: "user", content: "question" },
      { id: "reasoning-1", role: "reasoning", content: "先并行调用。" },
      {
        id: "assistant-tools",
        role: "assistant",
        content: "",
        toolCalls: [
          { id: "tc-1", function: { name: "list_data_sources" } },
          { id: "tc-2", function: { name: "list_data_sources" } },
        ],
      },
    ];

    expect(
      reasoningMessageAbsorbedByFollowingToolStep(messages[1], messages),
    ).toBe(true);
  });

  it("keeps render-only assistant preambles that are missing from agent messages", () => {
    const preamble = "我将并行调用 list_data_sources 三次。";
    const agentMessages = [
      { id: "user-1", role: "user", content: "同时执行三个list_data_sources" },
      {
        id: "assistant-tools",
        role: "assistant",
        content: "",
        toolCalls: [
          { id: "tc-1", function: { name: "list_data_sources" } },
          { id: "tc-2", function: { name: "list_data_sources" } },
          { id: "tc-3", function: { name: "list_data_sources" } },
        ],
      },
    ];
    const renderMessages = [
      { id: "user-1", role: "user", content: "同时执行三个list_data_sources" },
      { id: "assistant-preamble", role: "assistant", content: preamble },
      ...agentMessages.slice(1),
    ];

    const merged = mergeMessagesForStepContext(agentMessages, renderMessages);
    expect(
      resolveToolStepThoughtContent(
        merged.find((item) => item.id === "assistant-tools")!,
        merged,
      ),
    ).toBe(preamble);
  });
});
