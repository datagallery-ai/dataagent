import { describe, expect, it } from "vitest";
import type { SessionConversationDto } from "../../../lib/config-api/types";
import { resolveUserMessageBranchState } from "../conversation-branches";

const conversation: SessionConversationDto = {
  sessionId: "thread-branch",
  messages: [
    {
      id: "m1",
      runId: "run-1",
      role: "user",
      source: "client",
      messageId: "user-message-1",
      contentText: "Original question",
      position: 1,
      createdAt: "now",
    },
    {
      id: "m2",
      runId: "run-1",
      role: "assistant",
      source: "agent",
      contentText: "Original answer",
      position: 2,
      createdAt: "now",
    },
  ],
  runEventRefs: [],
  checkpoints: [
    {
      runId: "run-1",
      status: "completed",
      messageStartPosition: 1,
      messageEndPosition: 2,
      startedAt: "now",
      finishedAt: "later",
    },
  ],
  branches: [
    {
      sessionId: "thread-original",
      parentSessionId: "thread-original",
      rootSessionId: "thread-original",
      forkRunId: "run-1",
      forkMessageEndPosition: 0,
      isOriginal: true,
      createdAt: "now",
    },
    {
      sessionId: "thread-branch",
      parentSessionId: "thread-original",
      rootSessionId: "thread-original",
      forkRunId: "run-1",
      forkMessageEndPosition: 0,
      createdAt: "later",
    },
  ],
  toolCalls: [],
};

describe("conversation branches", () => {
  it("resolves branch controls for a completed user message", () => {
    const state = resolveUserMessageBranchState({
      activeSessionId: "thread-branch",
      conversation,
      messageId: "user-message-1",
    });

    expect(state).toMatchObject({
      branchable: true,
      currentIndex: 1,
      runId: "run-1",
      status: "completed",
      total: 2,
    });
    expect(state?.previousSessionId).toBe("thread-original");
    expect(state?.nextSessionId).toBe("thread-original");
  });

  it("resolves branch controls on a child branch session with a new run id", () => {
    const state = resolveUserMessageBranchState({
      activeSessionId: "thread-branch",
      conversation: {
        ...conversation,
        sessionId: "thread-branch",
        branch: {
          id: "branch:thread-branch",
          sessionId: "thread-branch",
          parentSessionId: "thread-original",
          rootSessionId: "thread-original",
          forkRunId: "run-1",
          forkMessageEndPosition: 0,
          createdAt: "later",
        },
        messages: [
          {
            id: "m-branch-user",
            runId: "run-branch-rewrite",
            role: "user",
            source: "client",
            messageId: "user-message-branch",
            contentText: "Rewritten question",
            position: 1,
            createdAt: "later",
          },
        ],
        checkpoints: [
          {
            runId: "run-branch-rewrite",
            status: "completed",
            messageStartPosition: 1,
            messageEndPosition: 1,
            startedAt: "later",
            finishedAt: "later",
          },
        ],
      },
      messageId: "user-message-branch",
    });

    expect(state).toMatchObject({
      branchable: true,
      currentIndex: 1,
      runId: "run-branch-rewrite",
      total: 2,
    });
    expect(state?.previousSessionId).toBe("thread-original");
  });

  it("does not show branch controls on later user messages in a child branch session", () => {
    const branchedConversation: SessionConversationDto = {
      ...conversation,
      sessionId: "thread-branch",
      branch: {
        id: "branch:thread-branch",
        sessionId: "thread-branch",
        parentSessionId: "thread-original",
        rootSessionId: "thread-original",
        forkRunId: "run-1",
        forkMessageEndPosition: 0,
        createdAt: "later",
      },
      messages: [
        {
          id: "m-branch-user",
          runId: "run-branch-rewrite",
          role: "user",
          source: "client",
          messageId: "user-message-branch",
          contentText: "Rewritten question",
          position: 1,
          createdAt: "later",
        },
        {
          id: "m-branch-assistant",
          runId: "run-branch-rewrite",
          role: "assistant",
          source: "agent",
          contentText: "Rewritten answer",
          position: 2,
          createdAt: "later",
        },
        {
          id: "m-follow-up-user",
          runId: "run-follow-up",
          role: "user",
          source: "client",
          messageId: "user-message-follow-up",
          contentText: "Follow-up question",
          position: 3,
          createdAt: "even-later",
        },
        {
          id: "m-follow-up-assistant",
          runId: "run-follow-up",
          role: "assistant",
          source: "agent",
          contentText: "Follow-up answer",
          position: 4,
          createdAt: "even-later",
        },
      ],
      checkpoints: [
        {
          runId: "run-branch-rewrite",
          status: "completed",
          messageStartPosition: 1,
          messageEndPosition: 2,
          startedAt: "later",
          finishedAt: "later",
        },
        {
          runId: "run-follow-up",
          status: "completed",
          messageStartPosition: 3,
          messageEndPosition: 4,
          startedAt: "even-later",
          finishedAt: "even-later",
        },
      ],
    };

    const forkPoint = resolveUserMessageBranchState({
      activeSessionId: "thread-branch",
      conversation: branchedConversation,
      messageId: "user-message-branch",
    });
    const followUp = resolveUserMessageBranchState({
      activeSessionId: "thread-branch",
      conversation: branchedConversation,
      messageId: "user-message-follow-up",
    });

    expect(forkPoint).toMatchObject({
      currentIndex: 1,
      total: 2,
    });
    expect(followUp).toMatchObject({
      total: 1,
    });
    expect(followUp?.previousSessionId).toBeUndefined();
    expect(followUp?.nextSessionId).toBeUndefined();
  });

  it("keeps branch controls only on the rewritten turn when a parent prefix is visible", () => {
    const branchedConversation: SessionConversationDto = {
      ...conversation,
      sessionId: "thread-branch",
      branch: {
        id: "branch:thread-branch",
        sessionId: "thread-branch",
        parentSessionId: "thread-original",
        rootSessionId: "thread-original",
        forkRunId: "run-2",
        forkMessageEndPosition: 2,
        createdAt: "later",
      },
      branches: [
        {
          sessionId: "thread-original",
          parentSessionId: "thread-original",
          rootSessionId: "thread-original",
          forkRunId: "run-2",
          forkMessageEndPosition: 2,
          isOriginal: true,
          createdAt: "now",
        },
        {
          sessionId: "thread-branch",
          parentSessionId: "thread-original",
          rootSessionId: "thread-original",
          forkRunId: "run-2",
          forkMessageEndPosition: 2,
          createdAt: "later",
        },
      ],
      messages: [
        {
          id: "m-prefix-user",
          runId: "run-1",
          role: "user",
          source: "client",
          messageId: "user-message-prefix",
          contentText: "Earlier question",
          position: 1,
          createdAt: "now",
        },
        {
          id: "m-prefix-assistant",
          runId: "run-1",
          role: "assistant",
          source: "agent",
          contentText: "Earlier answer",
          position: 2,
          createdAt: "now",
        },
        {
          id: "m-branch-user",
          runId: "run-branch-rewrite",
          role: "user",
          source: "client",
          messageId: "user-message-branch",
          contentText: "Rewritten later question",
          position: 3,
          createdAt: "later",
        },
        {
          id: "m-branch-assistant",
          runId: "run-branch-rewrite",
          role: "assistant",
          source: "agent",
          contentText: "Rewritten later answer",
          position: 4,
          createdAt: "later",
        },
        {
          id: "m-follow-up-user",
          runId: "run-follow-up",
          role: "user",
          source: "client",
          messageId: "user-message-follow-up",
          contentText: "Follow-up after rewrite",
          position: 5,
          createdAt: "even-later",
        },
      ],
      checkpoints: [
        {
          runId: "run-1",
          status: "completed",
          messageStartPosition: 1,
          messageEndPosition: 2,
          startedAt: "now",
          finishedAt: "now",
        },
        {
          runId: "run-branch-rewrite",
          status: "completed",
          messageStartPosition: 3,
          messageEndPosition: 4,
          startedAt: "later",
          finishedAt: "later",
        },
        {
          runId: "run-follow-up",
          status: "completed",
          messageStartPosition: 5,
          messageEndPosition: 5,
          startedAt: "even-later",
          finishedAt: "even-later",
        },
      ],
    };

    expect(resolveUserMessageBranchState({
      activeSessionId: "thread-branch",
      conversation: branchedConversation,
      messageId: "user-message-prefix",
    })).toMatchObject({ total: 1 });

    expect(resolveUserMessageBranchState({
      activeSessionId: "thread-branch",
      conversation: branchedConversation,
      messageId: "user-message-branch",
    })).toMatchObject({ total: 2, currentIndex: 1 });

    expect(resolveUserMessageBranchState({
      activeSessionId: "thread-branch",
      conversation: branchedConversation,
      messageId: "user-message-follow-up",
    })).toMatchObject({ total: 1 });
  });

  it("marks unfinished runs as refresh-only rewrites", () => {
    const state = resolveUserMessageBranchState({
      activeSessionId: "thread-live",
      conversation: {
        ...conversation,
        sessionId: "thread-live",
        checkpoints: [
          {
            runId: "run-1",
            status: "running",
            messageStartPosition: 1,
            messageEndPosition: 1,
            startedAt: "now",
          },
        ],
        branches: [],
      },
      messageId: "user-message-1",
    });

    expect(state).toMatchObject({
      branchable: false,
      refreshOnly: true,
      total: 1,
    });
  });
});
