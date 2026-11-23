import { describe, expect, it } from "vitest";
import {
  findLinkedCollaborationResponse,
  hasLaterAssistantMessage,
  resolveAssistantLiveToolCalls,
  resolveAssistantToolStepNumber,
  resolveStepAssistantFlags,
  shouldHideProcessStepForTimelineCollapse,
} from "../step-assistant-state";
import type { CollaborationResponseRecord } from "../components/chat/collaboration-responses";
import { createInitialLiveRun } from "../live-run-state";

const baseResponse: CollaborationResponseRecord = {
  id: "t:tc-ask",
  threadId: "t",
  toolCallId: "tc-ask",
  toolName: "ask_user",
  displayText: "继续测试",
  createdAt: 1,
  assistantMessageId: "msg-preamble",
};

describe("hasLaterAssistantMessage", () => {
  it("returns true when a later assistant message exists", () => {
    const messages = [
      { id: "msg-a", role: "assistant", content: "preamble" },
      { id: "msg-b", role: "assistant", toolCalls: [{ id: "tc-1" }] },
    ];
    expect(hasLaterAssistantMessage("msg-a", messages)).toBe(true);
    expect(hasLaterAssistantMessage("msg-b", messages)).toBe(false);
  });
});

describe("findLinkedCollaborationResponse", () => {
  it("links by assistantMessageId on orphan text messages", () => {
    const messages = [
      { id: "msg-preamble", role: "assistant", content: "Let's ask the user" },
    ];
    expect(findLinkedCollaborationResponse(messages[0], messages, [baseResponse])).toEqual(
      baseResponse,
    );
  });
});

describe("shouldHideProcessStepForTimelineCollapse", () => {
  it("hides process steps when the work process timeline is collapsed", () => {
    expect(
      shouldHideProcessStepForTimelineCollapse({
        isProcessStep: true,
        timelineCollapsed: true,
      }),
    ).toBe(true);
  });

  it("does not hide answers or expanded process steps", () => {
    expect(
      shouldHideProcessStepForTimelineCollapse({
        isProcessStep: false,
        timelineCollapsed: true,
      }),
    ).toBe(false);
    expect(
      shouldHideProcessStepForTimelineCollapse({
        isProcessStep: true,
        timelineCollapsed: false,
      }),
    ).toBe(false);
  });
});

describe("resolveStepAssistantFlags", () => {
  it("classifies orphan preambles as thought steps instead of streaming answers", () => {
    const messages = [
      { id: "msg-a", role: "assistant", content: "Let's update the task" },
      { id: "msg-b", role: "assistant", toolCalls: [{ id: "tc-1", function: { name: "task_update" } }] },
    ];
    const flags = resolveStepAssistantFlags({
      message: messages[0],
      messages,
      content: "Let's update the task",
      isRunning: true,
      liveRunStatus: "running",
      liveRun: createInitialLiveRun(),
      collaborationResponses: [],
    });

    expect(flags.isThought).toBe(true);
    expect(flags.isFinalAnswer).toBe(false);
    expect(flags.isActive).toBe(false);
  });

  it("marks ask_user preambles complete after the user responds", () => {
    const messages = [
      { id: "msg-preamble", role: "assistant", content: "Let's test ask_user" },
    ];
    const flags = resolveStepAssistantFlags({
      message: messages[0],
      messages,
      content: "Let's test ask_user",
      isRunning: true,
      liveRunStatus: "running",
      liveRun: createInitialLiveRun(),
      collaborationResponses: [baseResponse],
    });

    expect(flags.isCollaborationStep).toBe(false);
    expect(flags.isCollaborationComplete).toBe(true);
    expect(flags.isFinalAnswer).toBe(false);
    expect(flags.isActive).toBe(false);
    expect(flags.linkedCollaboration).toEqual(baseResponse);
  });

  it("keeps the last message in streaming answer mode when it is a true final answer", () => {
    const messages = [{ id: "msg-final", role: "assistant", content: "Done." }];
    const flags = resolveStepAssistantFlags({
      message: messages[0],
      messages,
      content: "Done.",
      isRunning: true,
      liveRunStatus: "running",
      liveRun: createInitialLiveRun(),
      collaborationResponses: [],
    });

    expect(flags.isFinalAnswer).toBe(true);
    expect(flags.isActive).toBe(true);
  });

  it("keeps completed final answers labeled as answers after a later user turn", () => {
    const messages = [
      { id: "user-1", role: "user", content: "你好" },
      { id: "assistant-1", role: "assistant", content: "你好！很高兴为你提供帮助。" },
      { id: "user-2", role: "user", content: "你好" },
      { id: "assistant-2", role: "assistant", content: "你好！很高兴再次见到你。" },
    ];
    const firstTurnFlags = resolveStepAssistantFlags({
      message: messages[1],
      messages,
      content: "你好！很高兴为你提供帮助。",
      isRunning: false,
      liveRunStatus: "completed",
      liveRun: createInitialLiveRun(),
      collaborationResponses: [],
    });
    const secondTurnFlags = resolveStepAssistantFlags({
      message: messages[3],
      messages,
      content: "你好！很高兴再次见到你。",
      isRunning: false,
      liveRunStatus: "completed",
      liveRun: createInitialLiveRun(),
      collaborationResponses: [],
    });

    expect(firstTurnFlags.isFinalAnswer).toBe(true);
    expect(firstTurnFlags.isThought).toBe(false);
    expect(secondTurnFlags.isFinalAnswer).toBe(true);
    expect(secondTurnFlags.isThought).toBe(false);
  });

  it("does not classify a final text answer as collaboration only because the live run has a collaboration tool", () => {
    const messages = [
      { id: "msg-final", role: "assistant", content: "收到，计划已批准，可以继续执行。" },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        {
          id: "tc-plan",
          name: "submit_plan",
          status: "success" as const,
          startedAtMs: 1,
          finishedAtMs: 2,
        },
      ],
    };
    const flags = resolveStepAssistantFlags({
      message: messages[0],
      messages,
      content: "收到，计划已批准，可以继续执行。",
      isRunning: false,
      liveRunStatus: "completed",
      liveRun,
      collaborationResponses: [],
    });

    expect(flags.isCollaborationStep).toBe(false);
    expect(flags.isFinalAnswer).toBe(true);
  });

  it("does not let a previous collaboration tool relabel a later run's named data tool", () => {
    const messages = [
      { id: "user-1", role: "user", content: "测试 ask_user" },
      { id: "assistant-1", role: "assistant", content: "请选择工具" },
      { id: "user-2", role: "user", content: "继续调用数据源工具" },
      {
        id: "assistant-2",
        role: "assistant",
        content: "我将先列出数据源。",
        toolCalls: [{ id: "tc-list", function: { name: "list_data_sources" } }],
      },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        {
          id: "tc-ask",
          name: "ask_user",
          status: "success" as const,
          startedAtMs: 1,
          finishedAtMs: 2,
        },
        {
          id: "tc-list",
          name: "list_data_sources",
          status: "running" as const,
          startedAtMs: 3,
        },
      ],
    };
    const flags = resolveStepAssistantFlags({
      message: messages[3],
      messages,
      content: "我将先列出数据源。",
      isRunning: true,
      liveRunStatus: "running",
      liveRun,
      collaborationResponses: [],
    });

    expect(flags.isCollaborationStep).toBe(false);
    expect(flags.hasToolCalls).toBe(true);
  });

  it("does not keep the last step streaming after ask_user suspends the run", () => {
    const messages = [
      { id: "assistant-ask", role: "assistant", content: "请选择下一步" },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        {
          id: "tc-ask",
          name: "ask_user",
          status: "running" as const,
          startedAtMs: 1,
        },
      ],
    };
    const flags = resolveStepAssistantFlags({
      message: messages[0],
      messages,
      content: "请选择下一步",
      isRunning: true,
      liveRunStatus: "suspended",
      liveRun,
      collaborationResponses: [],
    });

    expect(flags.isWaitingForUser).toBe(true);
    expect(flags.isActive).toBe(false);
    expect(flags.isFinalAnswer).toBe(false);
  });

  it("recognizes collaboration steps when chat tool names are stale but live run is authoritative", () => {
    const messages = [
      { id: "user-1", role: "user", content: "question" },
      {
        id: "assistant-ask",
        role: "assistant",
        content: "",
        toolCalls: [{ id: "tc-ask", function: { name: "tool" } }],
      },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        {
          id: "tc-ask",
          name: "ask_user",
          status: "running" as const,
          startedAtMs: 1,
        },
      ],
    };
    const flags = resolveStepAssistantFlags({
      message: messages[1],
      messages,
      content: "",
      isRunning: true,
      liveRunStatus: "suspended",
      liveRun,
      collaborationResponses: [],
    });

    expect(flags.isCollaborationStep).toBe(true);
    expect(flags.isWaitingForUser).toBe(true);
  });

  it("does not infer collaboration on a preamble when a later message already answered HITL", () => {
    const messages = [
      { id: "assistant-preamble", role: "assistant", content: "我先提交计划。" },
      { id: "assistant-plan", role: "assistant", content: "" },
      { id: "assistant-answer", role: "assistant", content: "计划已批准。" },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        {
          id: "tc-plan",
          name: "submit_plan",
          status: "success" as const,
          startedAtMs: 1,
          finishedAtMs: 2,
        },
      ],
    };
    const flags = resolveStepAssistantFlags({
      message: messages[0],
      messages,
      content: "我先提交计划。",
      isRunning: false,
      liveRunStatus: "completed",
      liveRun,
      collaborationResponses: [
        {
          ...baseResponse,
          toolCallId: "tc-plan",
          toolName: "submit_plan",
          displayText: "批准",
          assistantMessageId: "assistant-plan",
        },
      ],
    });

    expect(flags.isCollaborationStep).toBe(false);
    expect(flags.isThought).toBe(true);
  });

  it("does not infer ask_user on a preamble when a later assistant message owns the tool call", () => {
    const messages = [
      { id: "assistant-preamble", role: "assistant", content: "我需要确认你的选择。" },
      {
        id: "assistant-ask",
        role: "assistant",
        toolCalls: [{ id: "tc-ask", function: { name: "ask_user" } }],
      },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        {
          id: "tc-ask",
          name: "ask_user",
          status: "running" as const,
          startedAtMs: 1,
        },
      ],
    };
    const preambleFlags = resolveStepAssistantFlags({
      message: messages[0],
      messages,
      content: "我需要确认你的选择。",
      isRunning: true,
      liveRunStatus: "suspended",
      liveRun,
      collaborationResponses: [],
    });
    const askFlags = resolveStepAssistantFlags({
      message: messages[1],
      messages,
      content: "",
      isRunning: true,
      liveRunStatus: "suspended",
      liveRun,
      collaborationResponses: [],
    });

    expect(preambleFlags.isCollaborationStep).toBe(false);
    expect(preambleFlags.isThought).toBe(true);
    expect(askFlags.isWaitingForUser).toBe(true);
    expect(askFlags.isCollaborationStep).toBe(true);
  });

  it("splits post-resume text from the HITL tool step card", () => {
    const messages = [
      {
        id: "assistant-ask",
        role: "assistant",
        content: "收到，用户选择了选项A。",
        toolCalls: [{ id: "tc-ask", function: { name: "ask_user" } }],
      },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        {
          id: "tc-ask",
          name: "ask_user",
          status: "success" as const,
          startedAtMs: 1,
          finishedAtMs: 2,
        },
      ],
    };
    const flags = resolveStepAssistantFlags({
      message: messages[0],
      messages,
      content: "收到，用户选择了选项A。",
      isRunning: false,
      liveRunStatus: "completed",
      liveRun,
      collaborationResponses: [
        {
          id: "t:tc-ask",
          threadId: "t",
          toolCallId: "tc-ask",
          toolName: "ask_user",
          displayText: "选项A",
          createdAt: 1,
          assistantMessageId: "assistant-ask",
        },
      ],
    });

    expect(flags.isCollaborationFollowUpAnswer).toBe(true);
    expect(flags.isFinalAnswer).toBe(false);
    expect(flags.isThought).toBe(false);
    expect(flags.isFollowUpAnswerActive).toBe(false);
  });

  it("does not show waiting state after collaboration was already answered", () => {
    const messages = [
      { id: "assistant-1", role: "assistant", toolCalls: [{ id: "tc-data", function: { name: "list_data_sources" } }] },
      { id: "assistant-ask", role: "assistant", content: "请选择下一步" },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      runStatus: "suspended" as const,
      toolCalls: [
        {
          id: "tc-data",
          name: "list_data_sources",
          status: "success" as const,
          startedAtMs: 1,
        },
        {
          id: "tc-ask",
          name: "ask_user",
          status: "success" as const,
          startedAtMs: 2,
        },
      ],
    };
    const flags = resolveStepAssistantFlags({
      message: messages[1],
      messages,
      content: "请选择下一步",
      isRunning: false,
      liveRunStatus: "suspended",
      liveRun,
      collaborationResponses: [
        {
          id: "t:tc-ask",
          threadId: "t",
          toolCallId: "tc-ask",
          toolName: "ask_user",
          displayText: "orders",
          createdAt: 1,
          assistantMessageId: "assistant-ask",
        },
      ],
    });

    expect(flags.isWaitingForUser).toBe(false);
    expect(flags.isCollaborationComplete).toBe(true);
  });
});

describe("resolveAssistantToolStepNumber", () => {
  it("keeps regular tool-call messages in the numbered sequence", () => {
    const messages = [
      { id: "assistant-1", role: "assistant", toolCalls: [{ id: "tc-data", function: { name: "list_data_sources" } }] },
      { id: "assistant-2", role: "assistant", toolCalls: [{ id: "tc-sql", function: { name: "run_sql_readonly" } }] },
    ];

    expect(
      resolveAssistantToolStepNumber({
        message: messages[1],
        messages,
        liveRun: createInitialLiveRun(),
        collaborationResponses: [],
      }),
    ).toBe(2);
  });

  it("counts parallel tool calls in one assistant message as one process step", () => {
    const messages = [
      {
        id: "assistant-1",
        role: "assistant",
        toolCalls: [
          { id: "tc-list", function: { name: "list_data_sources" } },
          { id: "tc-schema", function: { name: "inspect_schema" } },
        ],
      },
      {
        id: "assistant-2",
        role: "assistant",
        toolCalls: [{ id: "tc-sql", function: { name: "run_sql_readonly" } }],
      },
    ];

    expect(
      resolveAssistantToolStepNumber({
        message: messages[0],
        messages,
        liveRun: createInitialLiveRun(),
        collaborationResponses: [],
      }),
    ).toBe(1);
    expect(
      resolveAssistantToolStepNumber({
        message: messages[1],
        messages,
        liveRun: createInitialLiveRun(),
        collaborationResponses: [],
      }),
    ).toBe(2);
  });

  it("numbers an answered ask_user step even when the chat message has no toolCalls", () => {
    const messages = [
      { id: "assistant-1", role: "assistant", toolCalls: [{ id: "tc-data", function: { name: "list_data_sources" } }] },
      { id: "assistant-ask", role: "assistant", content: "请选择下一步" },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        {
          id: "tc-data",
          name: "list_data_sources",
          status: "success" as const,
          startedAtMs: 1,
          finishedAtMs: 2,
        },
        {
          id: "tc-ask",
          name: "ask_user",
          status: "success" as const,
          startedAtMs: 3,
          finishedAtMs: 4,
        },
      ],
    };

    expect(
      resolveAssistantToolStepNumber({
        message: messages[1],
        messages,
        liveRun,
        collaborationResponses: [
          {
            ...baseResponse,
            toolCallId: "tc-ask",
            assistantMessageId: "assistant-ask",
          },
        ],
      }),
    ).toBe(2);
  });

  it("numbers a pending submit_plan step inferred from the live run", () => {
    const messages = [
      { id: "assistant-1", role: "assistant", toolCalls: [{ id: "tc-data", function: { name: "list_data_sources" } }] },
      { id: "assistant-plan", role: "assistant", content: "" },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        {
          id: "tc-data",
          name: "list_data_sources",
          status: "success" as const,
          startedAtMs: 1,
          finishedAtMs: 2,
        },
        {
          id: "tc-plan",
          name: "submit_plan",
          status: "running" as const,
          startedAtMs: 3,
        },
      ],
    };

    expect(
      resolveAssistantToolStepNumber({
        message: messages[1],
        messages,
        liveRun,
        collaborationResponses: [],
      }),
    ).toBe(2);
  });

  it("restarts step numbering within each user turn", () => {
    const messages = [
      { id: "user-1", role: "user", content: "第一轮" },
      {
        id: "assistant-1",
        role: "assistant",
        toolCalls: [{ id: "tc-1", function: { name: "list_data_sources" } }],
      },
      { id: "user-2", role: "user", content: "第二轮" },
      {
        id: "assistant-2",
        role: "assistant",
        toolCalls: [{ id: "tc-2", function: { name: "inspect_schema" } }],
      },
    ];

    expect(
      resolveAssistantToolStepNumber({
        message: messages[3],
        messages,
        liveRun: createInitialLiveRun(),
        collaborationResponses: [],
      }),
    ).toBe(1);
  });
});

describe("resolveAssistantLiveToolCalls", () => {
  it("maps a streaming thinking-only assistant message to its live running tool call", () => {
    const messages = [
      { id: "user-1", role: "user", content: "查一下订单表" },
      { id: "assistant-thinking", role: "assistant", content: "我先检查 schema。" },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      toolCalls: [
        {
          id: "tc-schema",
          name: "inspect_schema",
          status: "running" as const,
          startedAtMs: 10,
        },
      ],
    };

    expect(
      resolveAssistantLiveToolCalls({
        message: messages[1],
        messages,
        liveRun,
      }),
    ).toEqual([
      {
        id: "tc-schema",
        name: "inspect_schema",
        status: "running",
        startedAtMs: 10,
      },
    ]);
  });
});
