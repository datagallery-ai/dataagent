import { describe, expect, it } from "vitest";
import { createInitialLiveRun } from "../live-run-state";
import {
  canResumeCollaborationInterrupt,
  findPendingCollaborationToolCall,
  reconcileSuspendedLiveRunState,
  messageHostsPendingCollaborationSlot,
  resolveCollaborationRecapAnchorMessageId,
  resolvePendingInterruptAnchorMessageId,
  shouldShowCollaborationRecap,
  shouldShowCollaborationRecapOnMessage,
  shouldShowPendingInterruptOnMessage,
} from "../collaboration-recap";
import type { LiveRun } from "../live-run-state";

const baseAskResponse = {
  id: "t:tc-ask",
  threadId: "t",
  toolCallId: "tc-ask",
  toolName: "ask_user" as const,
  question: "选择数据源",
  displayText: "orders",
  createdAt: 1,
  assistantMessageId: "msg-ask",
};

describe("shouldShowCollaborationRecap", () => {
  it("keeps recap visible after the collaboration tool completes", () => {
    const response = {
      id: "t:tc-plan",
      threadId: "t",
      toolCallId: "tc-plan",
      toolName: "submit_plan" as const,
      question: "执行Plan approval",
      plan: "## Step 1",
      displayText: "Plan approved",
      createdAt: 1,
    };
    const message = {
      id: "msg-1",
      role: "assistant" as const,
      toolCalls: [{ id: "tc-plan", function: { name: "submit_plan" } }],
    };
    const liveRun = {
      toolCalls: [{ id: "tc-plan", name: "submit_plan", status: "success" }],
    } as LiveRun;

    expect(shouldShowCollaborationRecap(message, response, liveRun)).toBe(true);
  });
});

describe("resolveCollaborationRecapAnchorMessageId", () => {
  it("prefers assistantMessageId over later messages that also carry the tool call", () => {
    const messages = [
      {
        id: "msg-ask",
        role: "assistant",
        toolCalls: [{ id: "tc-ask", function: { name: "ask_user" } }],
      },
      {
        id: "msg-trailing",
        role: "assistant",
        toolCalls: [{ id: "tc-ask", function: { name: "ask_user" } }],
      },
    ];

    expect(resolveCollaborationRecapAnchorMessageId(baseAskResponse, messages)).toBe("msg-ask");
    expect(shouldShowCollaborationRecapOnMessage(messages[0], baseAskResponse, messages)).toBe(
      true,
    );
    expect(shouldShowCollaborationRecapOnMessage(messages[1], baseAskResponse, messages)).toBe(
      false,
    );
  });
});

describe("resolvePendingInterruptAnchorMessageId", () => {
  it("anchors pending interrupt to the assistant message carrying the tool call", () => {
    const messages = [
      {
        id: "msg-ask",
        role: "assistant",
        toolCalls: [{ id: "tc-ask", function: { name: "ask_user" } }],
      },
      {
        id: "msg-trailing",
        role: "assistant",
        content: "waiting",
      },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      runStatus: "suspended" as const,
      toolCalls: [{ id: "tc-ask", name: "ask_user", status: "running" as const, startedAtMs: 1 }],
    };

    expect(
      resolvePendingInterruptAnchorMessageId("tc-ask", messages, liveRun, liveRun.runStatus),
    ).toBe("msg-ask");
    expect(
      shouldShowPendingInterruptOnMessage(
        messages[0],
        "tc-ask",
        messages,
        liveRun,
        liveRun.runStatus,
      ),
    ).toBe(true);
    expect(
      shouldShowPendingInterruptOnMessage(
        messages[1],
        "tc-ask",
        messages,
        liveRun,
        liveRun.runStatus,
      ),
    ).toBe(false);
  });

  it("falls back to the last assistant message while tool calls are not yet attached", () => {
    const messages = [
      { id: "msg-user", role: "user", content: "go" },
      { id: "msg-assistant", role: "assistant", content: "" },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      runStatus: "suspended" as const,
      toolCalls: [{ id: "tc-plan", name: "submit_plan", status: "running" as const, startedAtMs: 1 }],
    };

    expect(
      resolvePendingInterruptAnchorMessageId("tc-plan", messages, liveRun, liveRun.runStatus),
    ).toBe("msg-assistant");
  });

  it("falls back to the last message when suspend happens before assistant bubble lands", () => {
    const messages = [{ id: "msg-user", role: "user", content: "go" }];
    const liveRun = {
      ...createInitialLiveRun(),
      runStatus: "suspended" as const,
      toolCalls: [{ id: "tc-ask", name: "ask_user", status: "running" as const, startedAtMs: 1 }],
    };

    expect(
      resolvePendingInterruptAnchorMessageId("tc-ask", messages, liveRun, liveRun.runStatus),
    ).toBe("msg-user");
    expect(
      shouldShowPendingInterruptOnMessage(
        messages[0],
        "tc-ask",
        messages,
        liveRun,
        liveRun.runStatus,
      ),
    ).toBe(true);
  });
});

describe("messageHostsPendingCollaborationSlot", () => {
  it("hosts on the anchored assistant message", () => {
    const messages = [
      {
        id: "msg-ask",
        role: "assistant",
        toolCalls: [{ id: "tc-ask", function: { name: "ask_user" } }],
      },
    ];
    const liveRun = {
      ...createInitialLiveRun(),
      runStatus: "suspended" as const,
      toolCalls: [{ id: "tc-ask", name: "ask_user", status: "running" as const, startedAtMs: 1 }],
    };

    expect(
      messageHostsPendingCollaborationSlot(
        messages[0],
        "tc-ask",
        messages,
        liveRun,
        liveRun.runStatus,
      ),
    ).toBe(true);
  });

  it("hosts on the last message when suspend happens before assistant bubble lands", () => {
    const messages = [{ id: "msg-user", role: "user", content: "go" }];
    const liveRun = {
      ...createInitialLiveRun(),
      runStatus: "suspended" as const,
      toolCalls: [{ id: "tc-ask", name: "ask_user", status: "running" as const, startedAtMs: 1 }],
    };

    expect(
      messageHostsPendingCollaborationSlot(
        messages[0],
        "tc-ask",
        messages,
        liveRun,
        liveRun.runStatus,
      ),
    ).toBe(true);
  });
});

describe("findPendingCollaborationToolCall", () => {
  it("returns unanswered collaboration tool in the current suspended segment", () => {
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
          status: "running" as const,
          startedAtMs: 2,
        },
      ],
    };

    expect(findPendingCollaborationToolCall(liveRun, [])).toMatchObject({ id: "tc-ask" });
    expect(findPendingCollaborationToolCall(liveRun, [baseAskResponse])).toBeUndefined();
  });
});

describe("reconcileSuspendedLiveRunState", () => {
  it("clears stale suspended status when collaboration was already answered", () => {
    const liveRun = {
      ...createInitialLiveRun(),
      runStatus: "suspended" as const,
      toolCalls: [
        {
          id: "tc-ask",
          name: "ask_user",
          status: "success" as const,
          startedAtMs: 1,
        },
        {
          id: "tc-schema",
          name: "inspect_schema",
          status: "success" as const,
          startedAtMs: 2,
        },
      ],
    };

    expect(reconcileSuspendedLiveRunState(liveRun, [baseAskResponse]).runStatus).toBe("completed");
    expect(
      reconcileSuspendedLiveRunState(liveRun, [baseAskResponse]).toolCalls.every(
        (call) => call.status !== "running",
      ),
    ).toBe(true);
  });

  it("keeps suspended status while collaboration is still pending", () => {
    const liveRun = {
      ...createInitialLiveRun(),
      runStatus: "suspended" as const,
      toolCalls: [
        {
          id: "tc-ask",
          name: "ask_user",
          status: "running" as const,
          startedAtMs: 1,
        },
      ],
    };

    expect(reconcileSuspendedLiveRunState(liveRun, []).runStatus).toBe("suspended");
  });

  it("keeps suspended status before tool-call events land in LiveRun", () => {
    const liveRun = {
      ...createInitialLiveRun(),
      runStatus: "suspended" as const,
      toolCalls: [],
    };

    expect(reconcileSuspendedLiveRunState(liveRun, []).runStatus).toBe("suspended");
  });
});

describe("canResumeCollaborationInterrupt", () => {
  it("blocks resume when the tool was already answered", () => {
    expect(
      canResumeCollaborationInterrupt({
        toolCallId: "tc-ask",
        collaborationResponses: [baseAskResponse],
        liveRun: {
          ...createInitialLiveRun(),
          runStatus: "suspended",
          toolCalls: [
            {
              id: "tc-ask",
              name: "ask_user",
              status: "success",
              startedAtMs: 1,
            },
          ],
        },
        liveRunStatus: "suspended",
      }),
    ).toBe(false);
  });

  it("allows resume only for the pending collaboration tool", () => {
    const liveRun = {
      ...createInitialLiveRun(),
      runStatus: "suspended" as const,
      toolCalls: [
        {
          id: "tc-ask",
          name: "ask_user",
          status: "running" as const,
          startedAtMs: 1,
        },
      ],
    };

    expect(
      canResumeCollaborationInterrupt({
        toolCallId: "tc-ask",
        collaborationResponses: [],
        liveRun,
        liveRunStatus: "suspended",
      }),
    ).toBe(true);
    expect(
      canResumeCollaborationInterrupt({
        toolCallId: "tc-other",
        collaborationResponses: [],
        liveRun,
        liveRunStatus: "suspended",
      }),
    ).toBe(false);
  });

  it("allows live resume while run is still running before suspend delta lands", () => {
    const liveRun = {
      ...createInitialLiveRun(),
      runStatus: "running" as const,
      toolCalls: [
        {
          id: "tc-ask",
          name: "ask_user",
          status: "running" as const,
          startedAtMs: 1,
        },
      ],
    };

    expect(
      canResumeCollaborationInterrupt({
        toolCallId: "tc-ask",
        collaborationResponses: [],
        liveRun,
        liveRunStatus: "running",
      }),
    ).toBe(true);
  });

  it("allows live resume when interrupt arrives before tool-call events", () => {
    expect(
      canResumeCollaborationInterrupt({
        toolCallId: "tc-ask",
        collaborationResponses: [],
        liveRun: {
          ...createInitialLiveRun(),
          runStatus: "running",
          toolCalls: [],
        },
        liveRunStatus: "running",
      }),
    ).toBe(true);
  });
});
