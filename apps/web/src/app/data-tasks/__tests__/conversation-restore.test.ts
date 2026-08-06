import { describe, expect, it } from "vitest";
import type { SessionConversationDto } from "../../../lib/config-api/types";
import {
  collaborationResponsesFromConversation,
  conversationToAgentMessages,
  hydrateLiveRunFromConversation,
  hydratePendingInteractionLiveRun,
  hydrateSessionUsageFromConversation,
  isIgnorableConversationRestoreError,
  isConversationRestoreRunActive,
  agentMessagesMatchConversation,
  latestUserQuestionFromConversation,
  pendingInteractionsFromConversation,
  replayRestorableCustomEvents,
  shouldHydrateLiveRunFromConversation,
  shouldRestoreConversation,
  shouldRestoreConversationMessages,
} from "../conversation-restore";
import { ConfigApiError } from "../../../lib/config-api/types";
import {
  createInitialLiveRun,
  deriveRunUsage,
  formatWorkspaceMetadataSummary,
  reconcileLiveRunArtifacts,
  reduceLiveRunEvent,
} from "../live-run-state";

describe("conversationToAgentMessages", () => {
  it("returns empty array when there are no messages", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [],
    };
    expect(conversationToAgentMessages(dto)).toEqual([]);
  });

  it("sorts by position and maps user/assistant roles with stable ids", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m2",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "Here is the answer.",
          position: 2,
          createdAt: "2026-06-25T10:00:02Z",
        },
        {
          id: "m1",
          runId: "run-1",
          role: "user",
          source: "client",
          messageId: "msg-user-1",
          contentText: "What is revenue?",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
    };

    expect(conversationToAgentMessages(dto)).toEqual([
      {
        id: "msg-user-1",
        role: "user",
        content: "What is revenue?",
      },
      {
        id: "msg-assistant-1",
        role: "assistant",
        content: "Here is the answer.",
      },
    ]);
  });

  it("restores folded reasoning contentParts onto assistant messages", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "user",
          source: "client",
          messageId: "msg-user-1",
          contentText: "分析 GMV",
          position: 1,
          createdAt: "2026-07-09T10:00:01Z",
        },
        {
          id: "m2",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "先检查 schema。",
          contentParts: [
            { type: "reasoning", text: "先检查 schema。" },
            { type: "text", text: "接下来查询订单表。" },
          ],
          position: 2,
          createdAt: "2026-07-09T10:00:02Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
    };

    expect(conversationToAgentMessages(dto)).toEqual([
      {
        id: "msg-user-1",
        role: "user",
        content: "分析 GMV",
      },
      {
        id: "msg-assistant-1",
        role: "assistant",
        content: [
          { type: "reasoning", text: "先检查 schema。" },
          { type: "text", text: "接下来查询订单表。" },
        ],
      },
    ]);
  });

  it("falls back to entry id when messageId is missing", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "persisted-user-1",
          runId: "run-1",
          role: "user",
          source: "client",
          contentText: "hello",
          position: 0,
          createdAt: "2026-06-25T10:00:00Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
    };

    expect(conversationToAgentMessages(dto)[0]?.id).toBe("persisted-user-1");
  });

  it("restores canceled runs with persisted assistant drafts", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "run-canceled:user",
          runId: "run-canceled",
          role: "user",
          source: "client",
          messageId: "frontend-user-canceled",
          contentText: "Stop after first observation",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
        {
          id: "run-canceled:assistant:partial",
          runId: "run-canceled",
          role: "assistant",
          source: "agent",
          messageId: "assistant-partial",
          contentText: "I inspected the first table before cancellation.",
          position: 2,
          createdAt: "2026-06-25T10:00:02Z",
        },
      ],
      runEventRefs: [{ runId: "run-canceled", eventCount: 2, firstSeq: 1, lastSeq: 2 }],
      checkpoints: [
        {
          runId: "run-canceled",
          status: "canceled",
          messageStartPosition: 1,
          messageEndPosition: 2,
          firstEventSeq: 1,
          lastEventSeq: 2,
          startedAt: "2026-06-25T10:00:00Z",
          finishedAt: "2026-06-25T10:00:03Z",
          errorMessage: "user-requested",
        },
      ],
      toolCalls: [],
    };

    expect(conversationToAgentMessages(dto)).toEqual([
      {
        id: "frontend-user-canceled",
        role: "user",
        content: "Stop after first observation",
      },
      {
        id: "assistant-partial",
        role: "assistant",
        content: "I inspected the first table before cancellation.",
      },
    ]);
  });

  it("does not insert assistant failure placeholders into restored chat messages", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m-user-failed",
          runId: "run-bad-model",
          role: "user",
          source: "client",
          messageId: "msg-user-failed",
          contentText: "用错误模型分析订单",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
        {
          id: "m-user-next",
          runId: "run-good-model",
          role: "user",
          source: "client",
          messageId: "msg-user-next",
          contentText: "换成正确模型继续",
          position: 2,
          createdAt: "2026-06-25T10:00:02Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
    };

    expect(conversationToAgentMessages(dto)).toEqual([
      {
        id: "msg-user-failed",
        role: "user",
        content: "用错误模型分析订单",
      },
      {
        id: "msg-user-next",
        role: "user",
        content: "换成正确模型继续",
      },
    ]);
  });

  it("skips empty content and unknown roles", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m-empty",
          runId: "run-1",
          role: "user",
          source: "client",
          contentText: "   ",
          position: 0,
          createdAt: "2026-06-25T10:00:00Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
    };

    expect(conversationToAgentMessages(dto)).toEqual([]);
  });

  it("attaches restored tool calls to the preceding assistant message", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "user",
          source: "client",
          messageId: "msg-user-1",
          contentText: "Inspect schema",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
        {
          id: "m2",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "I'll inspect the schema.",
          position: 2,
          createdAt: "2026-06-25T10:00:02Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-1",
          status: "completed",
          toolName: "inspect_schema",
          resultMessageId: "msg-tool-1",
          resultPreview: '{"tables":[]}',
        },
      ],
    };

    expect(conversationToAgentMessages(dto)).toEqual([
      {
        id: "msg-user-1",
        role: "user",
        content: "Inspect schema",
      },
      {
        id: "msg-assistant-1",
        role: "assistant",
        content: "I'll inspect the schema.",
        toolCalls: [
          {
            id: "tc-1",
            type: "function",
            function: {
              name: "inspect_schema",
              arguments: '{"tables":[]}',
            },
          },
        ],
      },
    ]);
  });

  it("uses persisted args instead of result preview for restored tool arguments", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "I'll inspect the schema.",
          position: 1,
          createdAt: "2026-06-25T10:00:02Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-1",
          status: "completed",
          toolName: "inspect_schema",
          args: { table_names: ["orders"] },
          resultPreview: '{"tables":[]}',
        },
      ],
    };

    expect(conversationToAgentMessages(dto)[0]?.toolCalls?.[0]?.function.arguments).toBe(
      JSON.stringify({ table_names: ["orders"] }),
    );
  });

  it("uses parentMessageId when linking tool calls to assistant messages", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "First step",
          position: 1,
          createdAt: "2026-06-25T10:00:02Z",
        },
        {
          id: "m2",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-2",
          contentText: "Second step",
          position: 2,
          createdAt: "2026-06-25T10:00:03Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-1",
          status: "completed",
          toolName: "list_data_sources",
          parentMessageId: "msg-assistant-1",
          callEventSeq: 1,
        },
        {
          runId: "run-1",
          toolCallId: "tc-2",
          status: "completed",
          toolName: "inspect_schema",
          parentMessageId: "msg-assistant-2",
          callEventSeq: 2,
        },
      ],
    };

    const restored = conversationToAgentMessages(dto);
    expect(restored[0]?.toolCalls?.[0]?.function.name).toBe("list_data_sources");
    expect(restored[1]?.toolCalls?.[0]?.function.name).toBe("inspect_schema");
  });

  it("distributes unlinked tool calls across assistant turns in order", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "user",
          source: "client",
          messageId: "msg-user-1",
          contentText: "analyze",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
        {
          id: "m2",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "Step 1",
          position: 2,
          createdAt: "2026-06-25T10:00:02Z",
        },
        {
          id: "m3",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-2",
          contentText: "Step 2",
          position: 3,
          createdAt: "2026-06-25T10:00:03Z",
        },
        {
          id: "m4",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-3",
          contentText: "Final answer",
          position: 4,
          createdAt: "2026-06-25T10:00:04Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-1",
          status: "completed",
          toolName: "list_data_sources",
          callEventSeq: 1,
        },
        {
          runId: "run-1",
          toolCallId: "tc-2",
          status: "completed",
          toolName: "inspect_schema",
          callEventSeq: 2,
        },
      ],
    };

    const restored = conversationToAgentMessages(dto);
    expect(restored.find((message) => message.id === "msg-assistant-1")?.toolCalls?.[0]?.function.name).toBe(
      "list_data_sources",
    );
    expect(restored.find((message) => message.id === "msg-assistant-2")?.toolCalls?.[0]?.function.name).toBe(
      "inspect_schema",
    );
    expect(restored.find((message) => message.id === "msg-assistant-3")?.toolCalls).toBeUndefined();
  });

  it("groups parallel tool calls that share an ephemeral parent message into one restored step", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m-user",
          runId: "run-1",
          role: "user",
          source: "client",
          messageId: "msg-user-1",
          contentText: "同时执行三个list_data_sources",
          position: 1,
          createdAt: "2026-06-30T10:00:01Z",
        },
        {
          id: "m-assistant-1",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "已确认数据源，接下来检查 schema。",
          position: 2,
          createdAt: "2026-06-30T10:00:02Z",
        },
        {
          id: "m-assistant-2",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-2",
          contentText: "schema 已检查完成。",
          position: 3,
          createdAt: "2026-06-30T10:00:03Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-list-1",
          status: "completed",
          toolName: "list_data_sources",
          parentMessageId: "msg-ephemeral-tools",
          callEventSeq: 8,
        },
        {
          runId: "run-1",
          toolCallId: "tc-list-2",
          status: "completed",
          toolName: "list_data_sources",
          parentMessageId: "msg-ephemeral-tools",
          callEventSeq: 11,
        },
        {
          runId: "run-1",
          toolCallId: "tc-list-3",
          status: "completed",
          toolName: "list_data_sources",
          parentMessageId: "msg-ephemeral-tools",
          callEventSeq: 14,
        },
        {
          runId: "run-1",
          toolCallId: "tc-schema",
          status: "completed",
          toolName: "inspect_schema",
          parentMessageId: "msg-assistant-1",
          callEventSeq: 46,
        },
      ],
    };

    const restored = conversationToAgentMessages(dto);
    const parallelStep = restored.find((message) =>
      message.toolCalls?.some((call) => call.id === "tc-list-1"),
    );
    expect(parallelStep?.toolCalls).toHaveLength(3);
    expect(parallelStep?.toolCalls?.map((call) => call.function.name)).toEqual([
      "list_data_sources",
      "list_data_sources",
      "list_data_sources",
    ]);
    expect(
      restored.find((message) => message.id === "msg-assistant-1")?.toolCalls?.[0]?.function.name,
    ).toBe("inspect_schema");
  });

  it("links tools to persisted empty tool-parent rows without synthetic orphans", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m-user",
          runId: "run-1",
          role: "user",
          source: "client",
          messageId: "msg-user-1",
          contentText: "写报告并发布",
          position: 1,
          createdAt: "2026-07-09T10:00:01Z",
        },
        {
          id: "m-write",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-write-parent",
          contentText: "",
          position: 2,
          createdAt: "2026-07-09T10:00:02Z",
        },
        {
          id: "m-publish",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-publish-parent",
          contentText: "",
          position: 3,
          createdAt: "2026-07-09T10:00:03Z",
        },
        {
          id: "m-final",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-final-answer",
          contentText: "报告已发布。",
          position: 4,
          createdAt: "2026-07-09T10:00:04Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-write",
          status: "completed",
          toolName: "write_file",
          parentMessageId: "msg-write-parent",
          callEventSeq: 10,
        },
        {
          runId: "run-1",
          toolCallId: "tc-publish",
          status: "completed",
          toolName: "publish_artifact",
          parentMessageId: "msg-publish-parent",
          callEventSeq: 20,
        },
      ],
    };

    const restored = conversationToAgentMessages(dto);
    expect(restored.map((message) => message.id)).toEqual([
      "msg-user-1",
      "msg-write-parent",
      "msg-publish-parent",
      "msg-final-answer",
    ]);
    expect(restored.some((message) => message.id.startsWith("restored-tool-parent:"))).toBe(false);
    expect(
      restored.find((message) => message.id === "msg-write-parent")?.toolCalls?.[0]?.function.name,
    ).toBe("write_file");
    expect(
      restored.find((message) => message.id === "msg-publish-parent")?.toolCalls?.[0]?.function.name,
    ).toBe("publish_artifact");
  });

  it("inserts orphan parent placeholders by callEventSeq instead of before the first assistant", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m-user",
          runId: "run-1",
          role: "user",
          source: "client",
          messageId: "msg-user-1",
          contentText: "对比两周 GMV 并输出报告",
          position: 1,
          createdAt: "2026-07-09T10:00:01Z",
        },
        {
          id: "m-sql",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-sql",
          contentText: "先查两周汇总。",
          position: 2,
          createdAt: "2026-07-09T10:00:02Z",
        },
        {
          id: "m-write",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-write",
          contentText: "开始写报告。",
          position: 3,
          createdAt: "2026-07-09T10:00:03Z",
        },
        {
          id: "m-final",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-final",
          contentText: "报告已发布。",
          position: 4,
          createdAt: "2026-07-09T10:00:04Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-schema",
          status: "completed",
          toolName: "inspect_schema",
          parentMessageId: "msg-ephemeral-early",
          callEventSeq: 8,
        },
        {
          runId: "run-1",
          toolCallId: "tc-sql",
          status: "completed",
          toolName: "run_sql_readonly",
          parentMessageId: "msg-sql",
          callEventSeq: 52,
        },
        {
          runId: "run-1",
          toolCallId: "tc-write",
          status: "completed",
          toolName: "write_file",
          parentMessageId: "msg-write",
          callEventSeq: 111,
        },
        {
          runId: "run-1",
          toolCallId: "tc-publish",
          status: "completed",
          toolName: "publish_artifact",
          parentMessageId: "msg-ephemeral-publish",
          callEventSeq: 119,
        },
      ],
    };

    const restored = conversationToAgentMessages(dto);
    const assistantToolOrder = restored
      .filter((message) => message.role === "assistant")
      .flatMap((message) =>
        (message.toolCalls ?? []).map((call) => call.function.name),
      );

    expect(assistantToolOrder).toEqual([
      "inspect_schema",
      "run_sql_readonly",
      "write_file",
      "publish_artifact",
    ]);

    const publishStepIndex = restored.findIndex((message) =>
      message.toolCalls?.some((call) => call.id === "tc-publish"),
    );
    const writeStepIndex = restored.findIndex((message) =>
      message.toolCalls?.some((call) => call.id === "tc-write"),
    );
    const finalIndex = restored.findIndex((message) => message.id === "msg-final");
    expect(writeStepIndex).toBeGreaterThan(-1);
    expect(publishStepIndex).toBeGreaterThan(writeStepIndex);
    expect(publishStepIndex).toBeLessThan(finalIndex);
  });

  it("restores empty assistant placeholders needed for collaboration tool anchoring", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m-user",
          runId: "run-1",
          role: "user",
          source: "user",
          messageId: "msg-user-1",
          contentText: "选择数据源",
          position: 0,
          createdAt: "2026-06-25T10:00:01Z",
        },
        {
          id: "m-ask",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-ask",
          contentText: "",
          position: 1,
          createdAt: "2026-06-25T10:00:02Z",
        },
        {
          id: "m-data",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-data",
          contentText: "Conversation Summary",
          position: 2,
          createdAt: "2026-06-25T10:00:03Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-ask",
          status: "completed",
          toolName: "ask_user",
          callEventSeq: 1,
          result: "orders",
        },
        {
          runId: "run-1",
          toolCallId: "tc-data",
          status: "completed",
          toolName: "list_data_sources",
          callEventSeq: 2,
        },
      ],
    };

    const restored = conversationToAgentMessages(dto);
    expect(restored.some((message) => message.id === "msg-assistant-ask")).toBe(true);
    expect(
      restored.find((message) => message.id === "msg-assistant-ask")?.toolCalls?.[0]?.function.name,
    ).toBe("ask_user");
    expect(
      restored.find((message) => message.id === "msg-assistant-data")?.toolCalls?.[0]?.function.name,
    ).toBe("list_data_sources");
  });
});

describe("collaborationResponsesFromConversation", () => {
  it("rebuilds answered ask_user records from persisted tool calls", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "请选择下一步",
          position: 1,
          createdAt: "2026-06-25T10:00:02Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-ask",
          status: "completed",
          toolName: "ask_user",
          args: {
            question: "继续哪种分析？",
            options: [{ label: "Inspect schema", value: "schema" }],
          },
          result: "schema",
        },
      ],
    };

    expect(collaborationResponsesFromConversation("thread-1", dto)).toEqual([
      {
        threadId: "thread-1",
        toolCallId: "tc-ask",
        toolName: "ask_user",
        question: "继续哪种分析？",
        displayText: "Inspect schema",
        assistantMessageId: "msg-assistant-1",
      },
    ]);
  });

  it("rebuilds ask_user records from the authoritative tool name", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "Conversation Summary",
          position: 1,
          createdAt: "2026-06-25T10:00:02Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-ask",
          status: "completed",
          toolName: "ask_user",
          result: { content: "User answered: orders" },
        },
      ],
    };

    expect(collaborationResponsesFromConversation("thread-1", dto)).toEqual([
      expect.objectContaining({
        threadId: "thread-1",
        toolCallId: "tc-ask",
        toolName: "ask_user",
        displayText: "orders",
      }),
    ]);
  });

  it("rebuilds submit_plan approval records", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "请审批计划",
          position: 1,
          createdAt: "2026-06-25T10:00:02Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-plan",
          status: "completed",
          toolName: "submit_plan",
          args: {
            title: "执行Plan approval",
            plan: "1. 查 schema\n2. 跑 SQL",
          },
          result: { action: "approved" },
        },
      ],
    };

    expect(collaborationResponsesFromConversation("thread-1", dto)).toEqual([
      {
        threadId: "thread-1",
        toolCallId: "tc-plan",
        toolName: "submit_plan",
        question: "执行Plan approval",
        plan: "1. 查 schema\n2. 跑 SQL",
        displayText: "Plan approved",
        assistantMessageId: "msg-assistant-1",
      },
    ]);
  });

  it("rebuilds submit_plan approval from the authoritative tool name", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "请审批计划",
          position: 1,
          createdAt: "2026-06-25T10:00:02Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-plan",
          status: "completed",
          toolName: "submit_plan",
          args: {
            title: "简单计划",
            plan: "步骤1：确认收到审批",
          },
          result: { action: "approved" },
        },
      ],
    };

    expect(collaborationResponsesFromConversation("thread-1", dto)).toEqual([
      expect.objectContaining({
        toolCallId: "tc-plan",
        toolName: "submit_plan",
        plan: "步骤1：确认收到审批",
        displayText: "Plan approved",
      }),
    ]);

    const restored = conversationToAgentMessages(dto);
    expect(
      restored.find((message) => message.id === "msg-assistant-1")?.toolCalls?.[0]?.function.name,
    ).toBe("submit_plan");

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(run.toolCalls.find((call) => call.id === "tc-plan")?.name).toBe("submit_plan");
  });

  it("rebuilds submit_plan approval from authoritative name and result", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "",
          position: 1,
          createdAt: "2026-06-25T10:00:02Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-plan",
          status: "completed",
          toolName: "submit_plan",
          resultPreview: JSON.stringify({
            action: "approved",
            content: "Plan approved",
            source: "mastra-collaboration",
          }),
          args: {
            title: "数据源计划",
            plan: "步骤1：调用list_data_sources",
          },
        },
      ],
    };

    expect(collaborationResponsesFromConversation("thread-1", dto)).toEqual([
      expect.objectContaining({
        toolCallId: "tc-plan",
        toolName: "submit_plan",
      }),
    ]);

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(run.toolCalls.find((call) => call.id === "tc-plan")?.name).toBe("submit_plan");
  });
});

describe("shouldRestoreConversation", () => {
  it("allows restore when conversation memory is on and chat is empty", () => {
    expect(
      shouldRestoreConversation({
        conversationMemoryEnabled: true,
        messageCount: 0,
        isRunning: false,
        alreadyRestored: false,
      }),
    ).toBe(true);
  });

  it("blocks restore when memory is off, chat has messages, run is active, or already restored", () => {
    const base = {
      conversationMemoryEnabled: true,
      messageCount: 0,
      isRunning: false,
      alreadyRestored: false,
    };

    expect(
      shouldRestoreConversation({ ...base, conversationMemoryEnabled: false }),
    ).toBe(false);
    expect(shouldRestoreConversation({ ...base, messageCount: 2 })).toBe(false);
    expect(shouldRestoreConversation({ ...base, isRunning: true })).toBe(false);
    expect(shouldRestoreConversation({ ...base, alreadyRestored: true })).toBe(
      false,
    );
  });
});

describe("isConversationRestoreRunActive", () => {
  it("waits when the Copilot agent is still running", () => {
    expect(
      isConversationRestoreRunActive({
        agentIsRunning: true,
        liveRunStatus: "completed",
      }),
    ).toBe(true);
  });

  it("waits while the page live run is running or suspended", () => {
    expect(
      isConversationRestoreRunActive({
        agentIsRunning: false,
        liveRunStatus: "running",
      }),
    ).toBe(true);
    expect(
      isConversationRestoreRunActive({
        agentIsRunning: false,
        liveRunStatus: "suspended",
      }),
    ).toBe(true);
  });

  it("allows restore after the page live run reaches a terminal or idle state", () => {
    for (const liveRunStatus of ["idle", "completed", "failed", "canceled"] as const) {
      expect(
        isConversationRestoreRunActive({
          agentIsRunning: false,
          liveRunStatus,
        }),
      ).toBe(false);
    }
  });
});

describe("agentMessagesMatchConversation", () => {
  const dto: SessionConversationDto = {
    sessionId: "thread-1",
    messages: [
      {
        id: "m1",
        runId: "run-1",
        role: "user",
        source: "client",
        messageId: "msg-user-1",
        contentText: "统计不同品类销售额",
        position: 1,
        createdAt: "2026-06-25T10:00:01Z",
      },
      {
        id: "m2",
        runId: "run-1",
        role: "assistant",
        source: "agent",
        messageId: "msg-assistant-1",
        contentText: "好的，我来分析。",
        position: 2,
        createdAt: "2026-06-25T10:00:02Z",
      },
    ],
    runEventRefs: [],
    toolCalls: [],
  };

  it("returns false when agent still holds another thread's messages", () => {
    expect(
      agentMessagesMatchConversation(
        [{ id: "other-thread-msg", role: "user", content: "hello" }],
        dto,
      ),
    ).toBe(false);
  });

  it("returns true when agent messages match restored conversation", () => {
    expect(agentMessagesMatchConversation(conversationToAgentMessages(dto), dto)).toBe(
      true,
    );
  });
});

describe("shouldRestoreConversationMessages", () => {
  it("restores when agent messages are stale after thread switch", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-2",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "user",
          source: "client",
          messageId: "msg-user-2",
          contentText: "查询 orders 表有多少行",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
    };

    expect(
      shouldRestoreConversationMessages({
        conversationMemoryEnabled: true,
        isRunning: false,
        replaceExistingMessages: true,
        agentMessages: [{ id: "msg-user-1", role: "user", content: "old question" }],
        dto,
      }),
    ).toBe(true);
  });

  it("does not overwrite a local trailing user message that has not been persisted yet", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "user",
          source: "client",
          messageId: "msg-user-1",
          contentText: "先查 orders 表",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
        {
          id: "m2",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "orders 表可以查询。",
          position: 2,
          createdAt: "2026-06-25T10:00:02Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
    };

    expect(
      shouldRestoreConversationMessages({
        conversationMemoryEnabled: true,
        isRunning: false,
        replaceExistingMessages: false,
        agentMessages: [
          ...conversationToAgentMessages(dto),
          {
            id: "local-user-message-bad-model",
            role: "user",
            content: "这条错误模型请求还没有写入服务端",
          },
        ],
        dto,
      }),
    ).toBe(false);
  });

  it("does not overwrite local answer messages when restored conversation is an older prefix", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "user",
          source: "client",
          messageId: "msg-user-1",
          contentText: "先查 orders 表",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
        {
          id: "m2",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-1",
          contentText: "orders 表可以查询。",
          position: 2,
          createdAt: "2026-06-25T10:00:02Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
    };

    expect(
      shouldRestoreConversationMessages({
        conversationMemoryEnabled: true,
        isRunning: false,
        replaceExistingMessages: false,
        agentMessages: [
          ...conversationToAgentMessages(dto),
          {
            id: "local-user-message-2",
            role: "user",
            content: "继续分析 GMV",
          },
          {
            id: "local-assistant-message-2",
            role: "assistant",
            content: "正在生成第二轮分析结论。",
          },
        ],
        dto,
      }),
    ).toBe(false);
  });

  it("does not replace an existing transcript during a same-thread background refresh", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "persisted-user-1",
          runId: "run-1",
          role: "user",
          source: "client",
          messageId: "persisted-user-message-1",
          contentText: "你好",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
    };

    expect(
      shouldRestoreConversationMessages({
        conversationMemoryEnabled: true,
        isRunning: false,
        replaceExistingMessages: false,
        agentMessages: [
          { id: "live-user-1", role: "user", content: "你好" },
          { id: "live-assistant-1", role: "assistant", content: "你好，有什么可以帮你？" },
          { id: "live-user-2", role: "user", content: "继续" },
        ],
        dto,
      }),
    ).toBe(false);
  });
});

describe("shouldHydrateLiveRunFromConversation", () => {
  it("hydrates when tool calls are missing but conversation has history", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "sql-1",
          status: "completed",
          toolName: "run_sql_readonly",
          callEventSeq: 1,
        },
      ],
    };

    expect(shouldHydrateLiveRunFromConversation(createInitialLiveRun(), dto)).toBe(true);
  });

  it("skips when live run already reflects the conversation", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "sql-1",
          status: "completed",
          toolName: "run_sql_readonly",
          callEventSeq: 1,
        },
      ],
    };

    let run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(shouldHydrateLiveRunFromConversation(run, dto)).toBe(false);
  });

  it("hydrates when AG-UI replay left the run running with no tool calls", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "sql-1",
          status: "completed",
          toolName: "run_sql_readonly",
          callEventSeq: 1,
        },
      ],
    };

    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    expect(run.runStatus).toBe("running");
    expect(shouldHydrateLiveRunFromConversation(run, dto)).toBe(true);
  });

  it("hydrates when replay only produced run boundaries without tools", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "schema-1",
          status: "completed",
          toolName: "inspect_schema",
          callEventSeq: 1,
          resultPreview: JSON.stringify({ tables: [] }),
        },
      ],
    };

    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: { id: "artifact-orphan", title: "旧产出", summary: "" },
    });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });

    expect(run.toolCalls).toHaveLength(0);
    expect((run.runHistory?.length ?? 0)).toBeGreaterThan(0);
    expect(shouldHydrateLiveRunFromConversation(run, dto)).toBe(true);

    run = hydrateLiveRunFromConversation(run, dto);
    expect(run.toolCalls).toHaveLength(1);
    expect(run.runHistory).toEqual([]);
  });

  it("hydrates when live run is missing tool ids from conversation", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-1",
          status: "completed",
          toolName: "list_data_sources",
          callEventSeq: 1,
        },
        {
          runId: "run-1",
          toolCallId: "tc-2",
          status: "completed",
          toolName: "inspect_schema",
          callEventSeq: 2,
        },
      ],
    };

    let run = createInitialLiveRun();
    run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_START",
      toolCallId: "tc-1",
      toolCallName: "list_data_sources",
    });
    run = reduceLiveRunEvent(run, {
      type: "TOOL_CALL_RESULT",
      toolCallId: "tc-1",
      toolCallName: "list_data_sources",
      result: "{}",
    });
    run = reduceLiveRunEvent(run, { type: "RUN_FINISHED" });

    expect(shouldHydrateLiveRunFromConversation(run, dto)).toBe(true);
    run = hydrateLiveRunFromConversation(run, dto);
    expect(run.toolCalls).toHaveLength(2);
  });

  it("hydrates user-only message runs as failed segments", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m-user-failed",
          runId: "run-bad-model",
          role: "user",
          source: "client",
          messageId: "msg-user-failed",
          contentText: "用错误模型分析订单",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
    };

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);

    expect(run.runStatus).toBe("failed");
    expect(run.errorMessage).toBe("Previous request failed before the assistant produced a response.");

    const nextRun = reduceLiveRunEvent(run, {
      type: "RUN_STARTED",
      runId: "run-good-model",
    });
    expect(nextRun.runStatus).toBe("running");
    expect(nextRun.errorMessage).toBeUndefined();
  });

  it("does not attach stale run-scoped diagnostics to the restored current run", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m-user-failed",
          runId: "run-bad-model",
          role: "user",
          source: "client",
          messageId: "msg-user-failed",
          contentText: "用错误模型分析订单",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
        {
          id: "m-user-good",
          runId: "run-good-model",
          role: "user",
          source: "client",
          messageId: "msg-user-good",
          contentText: "换成正确模型继续",
          position: 2,
          createdAt: "2026-06-25T10:00:02Z",
        },
        {
          id: "m-assistant-good",
          runId: "run-good-model",
          role: "assistant",
          source: "agent",
          messageId: "msg-assistant-good",
          contentText: "已恢复，可以继续分析。",
          position: 3,
          createdAt: "2026-06-25T10:00:03Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
      restorableCustomEvents: [
        {
          runId: "run-bad-model",
          seq: 1,
          name: "run.config.resolved",
          value: { activeLlmProfileId: "bad-model" },
        },
      ],
    };

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);

    expect(run.runId).toBe("run-good-model");
    expect(run.runStatus).toBe("completed");
    expect(run.resolvedRunConfig).toBeUndefined();
  });
});

describe("latestUserQuestionFromConversation", () => {
  it("returns the last user message text", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "user",
          source: "client",
          contentText: "first question",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
        {
          id: "m2",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          contentText: "answer",
          position: 2,
          createdAt: "2026-06-25T10:00:02Z",
        },
        {
          id: "m3",
          runId: "run-2",
          role: "user",
          source: "client",
          contentText: "follow up",
          position: 3,
          createdAt: "2026-06-25T10:00:03Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
    };

    expect(latestUserQuestionFromConversation(dto)).toBe("follow up");
  });

  it("skips collaboration echo user messages when resolving latest question", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-1",
          role: "user",
          source: "client",
          contentText: "先查看数据库，再问我下一步要做什么",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
        {
          id: "m2",
          runId: "run-2",
          role: "user",
          source: "client",
          contentText: "调用askuser tool",
          position: 2,
          createdAt: "2026-06-25T10:00:03Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
    };

    expect(latestUserQuestionFromConversation(dto)).toBe(
      "先查看数据库，再问我下一步要做什么",
    );
  });
});

describe("hydrateLiveRunFromConversation run ordering", () => {
  it("orders hydrated run groups by user turn instead of interleaved tool seq", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m1",
          runId: "run-a",
          role: "user",
          source: "client",
          contentText: "round one",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
        {
          id: "m2",
          runId: "run-b",
          role: "user",
          source: "client",
          contentText: "round two",
          position: 2,
          createdAt: "2026-06-25T10:00:02Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-b",
          toolCallId: "tc-collab",
          status: "completed",
          toolName: "ask_user",
          resultPreview: JSON.stringify({
            content: "User answered: yes",
            source: "mastra-collaboration",
          }),
        },
        {
          runId: "run-a",
          toolCallId: "tc-a",
          status: "completed",
          toolName: "list_data_sources",
          callEventSeq: 10,
        },
        {
          runId: "run-b",
          toolCallId: "tc-b",
          status: "completed",
          toolName: "inspect_schema",
          callEventSeq: 20,
        },
      ],
    };

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(run.toolCalls.map((call) => call.id)).toEqual([
      "tc-a",
      "tc-collab",
      "tc-b",
    ]);
    expect(run.toolCalls.find((call) => call.id === "tc-collab")?.name).toBe("ask_user");
    expect(run.runHistory).toHaveLength(1);
    expect(run.runHistory?.[0]?.toolCallEndIndex).toBe(1);
  });
});

describe("hydrateLiveRunFromConversation", () => {
  it("restores canceled message-only checkpoints as canceled live runs", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "run-canceled:user",
          runId: "run-canceled",
          role: "user",
          source: "client",
          messageId: "frontend-user-canceled",
          contentText: "Please write a long answer.",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
        {
          id: "run-canceled:assistant:partial",
          runId: "run-canceled",
          role: "assistant",
          source: "agent",
          messageId: "assistant-partial",
          contentText: "Partial answer before cancellation.",
          position: 2,
          createdAt: "2026-06-25T10:00:02Z",
        },
      ],
      runEventRefs: [{ runId: "run-canceled", eventCount: 12, firstSeq: 1, lastSeq: 12 }],
      checkpoints: [
        {
          runId: "run-canceled",
          status: "canceled",
          messageStartPosition: 1,
          messageEndPosition: 2,
          firstEventSeq: 1,
          lastEventSeq: 12,
          startedAt: "2026-06-25T10:00:00Z",
          finishedAt: "2026-06-25T10:00:03Z",
          errorMessage: "user-requested",
        },
      ],
      toolCalls: [],
    };

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(run.runStatus).toBe("canceled");
    expect(run.runStartedAt).toBe(Date.parse("2026-06-25T10:00:00Z"));
    expect(run.runFinishedAt).toBe(Date.parse("2026-06-25T10:00:03Z"));
    expect(deriveRunUsage(run).durationMs).toBe(3000);
  });

  it("restores canceled tool checkpoints with checkpoint timing", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [{ runId: "run-canceled", eventCount: 4, firstSeq: 1, lastSeq: 4 }],
      checkpoints: [
        {
          runId: "run-canceled",
          status: "canceled",
          messageStartPosition: 1,
          messageEndPosition: 2,
          firstEventSeq: 1,
          lastEventSeq: 4,
          startedAt: "2026-06-25T10:00:00Z",
          finishedAt: "2026-06-25T10:00:05Z",
          errorMessage: "user-requested",
        },
      ],
      toolCalls: [
        {
          runId: "run-canceled",
          toolCallId: "sql-canceled",
          status: "completed",
          toolName: "run_sql_readonly",
          callEventSeq: 2,
          resultEventSeq: 3,
          resultPreview: JSON.stringify({ row_count: 3 }),
        },
      ],
    };

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(run.runStatus).toBe("canceled");
    expect(run.runStartedAt).toBe(Date.parse("2026-06-25T10:00:00Z"));
    expect(run.runFinishedAt).toBe(Date.parse("2026-06-25T10:00:05Z"));
    expect(deriveRunUsage(run).durationMs).toBe(5000);
  });

  it("hydrates live run tool calls and links restored artifacts", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [{ runId: "run-1", eventCount: 4 }],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "sql-1",
          status: "completed",
          toolName: "run_sql_readonly",
          callEventSeq: 1,
          resultEventSeq: 2,
          resultPreview: JSON.stringify({ row_count: 3, elapsed_ms: 12 }),
        },
      ],
    };

    let run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(run.toolCalls).toHaveLength(1);
    expect(run.toolCalls[0]?.name).toBe("run_sql_readonly");
    expect(run.runStatus).toBe("completed");

    run = reduceLiveRunEvent(run, {
      type: "CUSTOM",
      name: "artifact",
      value: {
        id: "artifact-1",
        type: "table",
        title: "SQL result",
        preview_json: { row_count: 3, columns: ["a"], rows: [["1"]] },
      },
    });
    run = reconcileLiveRunArtifacts(run);
    expect(run.artifacts[0]?.createdByEventId).toBe("sql-1");
  });

  it("rebuilds multi-run tool history with run boundaries", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-1",
          status: "completed",
          toolName: "list_data_sources",
          callEventSeq: 1,
        },
        {
          runId: "run-1",
          toolCallId: "tc-ask",
          status: "pending",
          toolName: "ask_user",
          callEventSeq: 2,
        },
        {
          runId: "run-2",
          toolCallId: "tc-2",
          status: "completed",
          toolName: "inspect_schema",
          callEventSeq: 3,
          resultPreview: JSON.stringify({ tables: [] }),
        },
        {
          runId: "run-2",
          toolCallId: "tc-3",
          status: "completed",
          toolName: "run_sql_readonly",
          callEventSeq: 4,
          resultPreview: JSON.stringify({ row_count: 2 }),
        },
      ],
    };

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(run.toolCalls).toHaveLength(4);
    expect(run.runHistory).toHaveLength(1);
    expect(run.runHistory?.[0]?.status).toBe("suspended");
    expect(run.runHistory?.[0]?.toolCallEndIndex).toBe(2);
    expect(run.runStatus).toBe("completed");
  });

  it("restores a suspended run from the authoritative awaitingInteraction flag (R-018)", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-1",
          status: "completed",
          toolName: "list_data_sources",
          callEventSeq: 1,
        },
        {
          // Backend flags this HITL call as awaiting the user even though its status
          // is not "pending" — the authoritative flag alone must drive suspension.
          runId: "run-1",
          toolCallId: "tc-plan",
          status: "completed",
          toolName: "submit_plan",
          awaitingInteraction: true,
          callEventSeq: 2,
        },
      ],
    };

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(run.runStatus).toBe("suspended");
  });

  it("restores event-only completed checkpoints for user-only runs without assistant messages", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m-user-only",
          runId: "run-event-only",
          role: "user",
          source: "client",
          messageId: "msg-user-only",
          contentText: "Analyze orders",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
      ],
      runEventRefs: [{ runId: "run-event-only", eventCount: 8, firstSeq: 1, lastSeq: 8 }],
      checkpoints: [
        {
          runId: "run-event-only",
          status: "completed",
          messageStartPosition: 1,
          messageEndPosition: 1,
          firstEventSeq: 1,
          lastEventSeq: 8,
          terminalEvent: "RUN_FINISHED",
        },
      ],
      toolCalls: [],
    };

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(run.runStatus).toBe("completed");
    expect(run.errorMessage).toBeUndefined();
  });

  it("restores event-only failed checkpoints with authoritative error messages", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m-user-failed",
          runId: "run-event-only-failed",
          role: "user",
          source: "client",
          messageId: "msg-user-failed",
          contentText: "Use a bad model",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
      ],
      runEventRefs: [
        { runId: "run-event-only-failed", eventCount: 3, firstSeq: 1, lastSeq: 3 },
      ],
      checkpoints: [
        {
          runId: "run-event-only-failed",
          status: "failed",
          messageStartPosition: 1,
          messageEndPosition: 1,
          firstEventSeq: 1,
          lastEventSeq: 3,
          terminalEvent: "RUN_ERROR",
          errorMessage: "Model provider returned 503",
        },
      ],
      toolCalls: [],
    };

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(run.runStatus).toBe("failed");
    expect(run.errorMessage).toBe("Model provider returned 503");
  });

  it("hydrates checkpoint-only event runs without persisted messages or tool calls", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [{ runId: "run-events-only", eventCount: 2, firstSeq: 10, lastSeq: 11 }],
      checkpoints: [
        {
          runId: "run-events-only",
          status: "failed",
          firstEventSeq: 10,
          lastEventSeq: 11,
          terminalEvent: "RUN_ERROR",
          errorMessage: "Run terminated before assistant response",
        },
      ],
      toolCalls: [],
    };

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(run.runId).toBe("run-events-only");
    expect(run.runStatus).toBe("failed");
    expect(run.errorMessage).toBe("Run terminated before assistant response");
  });

  it("preserves orphaned artifacts when re-hydrating tool calls", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "sql-1",
          status: "completed",
          toolName: "run_sql_readonly",
          callEventSeq: 1,
          resultPreview: JSON.stringify({ row_count: 3 }),
        },
      ],
    };

    let polluted = createInitialLiveRun();
    polluted = reduceLiveRunEvent(polluted, { type: "RUN_STARTED" });
    polluted = reduceLiveRunEvent(polluted, {
      type: "CUSTOM",
      name: "artifact",
      value: { id: "artifact-orphan", title: "旧产出", summary: "" },
    });
    polluted = reduceLiveRunEvent(polluted, { type: "RUN_FINISHED" });

    const run = hydrateLiveRunFromConversation(polluted, dto);
    expect(run.toolCalls).toHaveLength(1);
    expect(run.artifacts.some((artifact) => artifact.id === "artifact-orphan")).toBe(true);
  });
});

describe("isIgnorableConversationRestoreError", () => {
  it("treats new-session not-found responses as empty history", () => {
    expect(
      isIgnorableConversationRestoreError(
        new ConfigApiError("RESOURCE_NOT_FOUND", "Session not found: thread-new", 404),
      ),
    ).toBe(true);
  });

  it("does not ignore non-404 restore failures", () => {
    expect(
      isIgnorableConversationRestoreError(
        new ConfigApiError("INTERNAL_ERROR", "database unavailable", 500),
      ),
    ).toBe(false);
  });
});

describe("hydratePendingInteractionLiveRun", () => {
  it("bootstraps suspended live run when only pendingInteractions are persisted", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [],
      pendingInteractions: [
        {
          interactionId: "int-1",
          runId: "run-1",
          toolCallId: "tc-ask",
          toolName: "ask_user",
          interruptEvent: {
            type: "mastra_suspend",
            toolCallId: "tc-ask",
            toolName: "ask_user",
          },
        },
      ],
    };

    const run = hydratePendingInteractionLiveRun(
      createInitialLiveRun(),
      "thread-1",
      dto,
      [],
    );

    expect(run.runStatus).toBe("suspended");
    expect(run.toolCalls).toEqual([
      expect.objectContaining({ id: "tc-ask", name: "ask_user", status: "running" }),
    ]);
  });

  it("does not need bootstrap when backend synthesizes awaitingInteraction toolCalls", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      checkpoints: [{ runId: "run-1", status: "suspended" }],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-ask",
          status: "pending",
          toolName: "ask_user",
          awaitingInteraction: true,
        },
      ],
      pendingInteractions: [
        {
          interactionId: "int-1",
          runId: "run-1",
          toolCallId: "tc-ask",
          toolName: "ask_user",
        },
      ],
    };

    const fromTools = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(fromTools.runStatus).toBe("suspended");
    expect(fromTools.toolCalls).toEqual([
      expect.objectContaining({ id: "tc-ask", name: "ask_user" }),
    ]);

    const afterBootstrap = hydratePendingInteractionLiveRun(
      fromTools,
      "thread-1",
      dto,
      [],
    );
    expect(afterBootstrap.runStatus).toBe("suspended");
    expect(afterBootstrap.toolCalls).toHaveLength(1);
  });
});

describe("pendingInteractionsFromConversation", () => {
  it("maps pending interactions with interrupt payloads", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [],
      pendingInteractions: [
        {
          interactionId: "int-1",
          runId: "run-1",
          toolCallId: "tc-ask",
          toolName: "ask_user",
          interruptEvent: {
            type: "mastra_suspend",
            toolCallId: "tc-ask",
            toolName: "ask_user",
            runId: "run-1",
          },
        },
      ],
    };

    expect(pendingInteractionsFromConversation("thread-1", dto)).toEqual([
      {
        threadId: "thread-1",
        runId: "run-1",
        toolCallId: "tc-ask",
        toolName: "ask_user",
        interruptEvent: {
          type: "mastra_suspend",
          toolCallId: "tc-ask",
          toolName: "ask_user",
          runId: "run-1",
        },
      },
    ]);
  });
});

describe("replayRestorableCustomEvents", () => {
  it("replays persisted custom events into live run state", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [],
      restorableCustomEvents: [
        {
          runId: "run-1",
          seq: 2,
          name: "token_usage",
          value: { input_tokens: 10, output_tokens: 5 },
        },
        {
          runId: "run-1",
          seq: 1,
          name: "sql_audit",
          value: { audit_log_id: "audit-1", status: "succeeded" },
        },
      ],
    };

    const run = replayRestorableCustomEvents(createInitialLiveRun(), dto);
    expect(run.audits).toHaveLength(1);
    expect(run.tokenUsageEvents).toHaveLength(1);
  });

  it("replays workspace and sandbox custom events scoped to the active run", () => {
    let run = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "RUN_STARTED",
      runId: "run-1",
    });
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [],
      runEventRefs: [],
      toolCalls: [],
      restorableCustomEvents: [
        {
          runId: "run-1",
          seq: 1,
          name: "workspace.metadata",
          value: { toolCallId: "tc-write", toolName: "write_file", path: "reports/summary.md" },
        },
        {
          runId: "run-2",
          seq: 2,
          name: "workspace.metadata",
          value: { toolCallId: "tc-other", toolName: "write_file", path: "other.md" },
        },
        {
          runId: "run-1",
          seq: 3,
          name: "sandbox.output",
          value: { kind: "stdout", text: "verify-ok\n" },
        },
      ],
    };

    run = replayRestorableCustomEvents(run, dto);
    expect(run.workspaceMetadata).toHaveLength(1);
    expect(run.workspaceMetadata[0]).toMatchObject({
      toolCallId: "tc-write",
      toolName: "write_file",
    });
    expect(run.sandboxOutputs).toHaveLength(1);
  });
});

describe("hydrateLiveRunFromConversation workspace signals", () => {
  it("restores workspace metadata from persisted workspace.metadata events", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-workspace",
      messages: [],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-write",
          status: "completed",
          toolName: "write_file",
          callEventSeq: 1,
          resultPreview: "Wrote 128 bytes to reports/summary.md",
        },
      ],
      restorableCustomEvents: [
        {
          runId: "run-1",
          seq: 1,
          name: "workspace.metadata",
          value: { toolCallId: "tc-write", toolName: "write_file", path: "reports/summary.md" },
        },
      ],
    };

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(run.workspaceMetadata).toHaveLength(1);
    expect(run.workspaceMetadata[0]).toMatchObject({
      toolCallId: "tc-write",
      toolName: "write_file",
    });
    expect(formatWorkspaceMetadataSummary(run.workspaceMetadata[0]!)).toContain(
      "reports/summary.md",
    );
  });

  it("keeps persisted workspace signals from earlier restored run segments", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-workspace-history",
      messages: [],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-write-1",
          status: "completed",
          toolName: "write_file",
          callEventSeq: 1,
          resultPreview: "Workspace write completed.",
        },
        {
          runId: "run-1",
          toolCallId: "tc-ask",
          status: "pending",
          toolName: "ask_user",
          callEventSeq: 2,
        },
        {
          runId: "run-2",
          toolCallId: "tc-write-2",
          status: "completed",
          toolName: "write_file",
          callEventSeq: 3,
          resultPreview: "Wrote 128 bytes to reports/second.md",
        },
      ],
      restorableCustomEvents: [
        {
          runId: "run-1",
          seq: 1,
          name: "workspace.metadata",
          value: {
            toolCallId: "tc-write-1",
            toolName: "write_file",
            path: "reports/from-custom-first.md",
          },
        },
        {
          runId: "run-2",
          seq: 2,
          name: "workspace.metadata",
          value: {
            toolCallId: "tc-write-2",
            toolName: "write_file",
            path: "reports/second.md",
          },
        },
      ],
    };

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    expect(
      run.workspaceMetadata
        .map((entry) => ({
          toolCallId: entry.toolCallId,
          path: (entry.payload as { path?: string }).path,
        }))
        .sort((left, right) => String(left.toolCallId).localeCompare(String(right.toolCallId))),
    ).toEqual([
      { toolCallId: "tc-write-1", path: "reports/from-custom-first.md" },
      { toolCallId: "tc-write-2", path: "reports/second.md" },
    ]);
  });

  it("restores sandbox outputs from persisted sandbox.output events", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-sandbox-history",
      messages: [],
      runEventRefs: [],
      toolCalls: [
        {
          runId: "run-1",
          toolCallId: "tc-command-1",
          status: "completed",
          toolName: "execute_command",
          callEventSeq: 1,
          resultPreview: "first-ok\n",
        },
        {
          runId: "run-1",
          toolCallId: "tc-command-2",
          status: "completed",
          toolName: "execute_command",
          callEventSeq: 2,
          resultPreview: "second-ok\n",
        },
      ],
      restorableCustomEvents: [
        {
          runId: "run-1",
          seq: 1,
          name: "sandbox.output",
          value: { kind: "stdout", text: "first-ok" },
        },
        {
          runId: "run-1",
          seq: 2,
          name: "sandbox.output",
          value: { kind: "stdout", text: "second-ok" },
        },
      ],
    };

    const run = hydrateLiveRunFromConversation(createInitialLiveRun(), dto);
    // The live reducer prepends sandbox outputs (newest-first); restore mirrors that order.
    expect(run.sandboxOutputs.map((output) => output.payload)).toEqual([
      { kind: "stdout", text: "second-ok" },
      { kind: "stdout", text: "first-ok" },
    ]);
  });
});

describe("hydrateSessionUsageFromConversation", () => {
  it("rebuilds cross-run token totals from restorable token usage events", () => {
    const dto: SessionConversationDto = {
      sessionId: "thread-1",
      messages: [
        {
          id: "m-user-1",
          runId: "run-1",
          role: "user",
          source: "client",
          contentText: "first",
          position: 1,
          createdAt: "2026-06-25T10:00:01Z",
        },
        {
          id: "m-assistant-1",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          contentText: "done",
          position: 2,
          createdAt: "2026-06-25T10:00:02Z",
        },
        {
          id: "m-user-2",
          runId: "run-2",
          role: "user",
          source: "client",
          contentText: "second",
          position: 3,
          createdAt: "2026-06-25T10:00:03Z",
        },
        {
          id: "m-assistant-2",
          runId: "run-2",
          role: "assistant",
          source: "agent",
          contentText: "done again",
          position: 4,
          createdAt: "2026-06-25T10:00:04Z",
        },
      ],
      runEventRefs: [],
      toolCalls: [],
      restorableCustomEvents: [
        {
          runId: "run-1",
          seq: 1,
          name: "token_usage",
          value: { input_tokens: 100, output_tokens: 20, model: "model-a" },
        },
        {
          runId: "run-2",
          seq: 2,
          name: "token_usage",
          value: { input_tokens: 300, output_tokens: 40, model: "model-b" },
        },
      ],
    };

    const usage = hydrateSessionUsageFromConversation(dto);
    expect(usage.runCount).toBe(2);
    expect(usage.completedRuns).toBe(2);
    expect(usage.tokens.inputTokens).toBe(400);
    expect(usage.tokens.outputTokens).toBe(60);
    expect(usage.models).toEqual(["model-a", "model-b"]);
  });
});
