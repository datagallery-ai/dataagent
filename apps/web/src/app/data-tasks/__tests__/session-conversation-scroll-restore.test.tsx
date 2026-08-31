/**
 * @vitest-environment happy-dom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import {
  findCopilotChatScrollContainer,
  restoreCopilotChatScrollTopWithRetries,
  scrollCopilotChatToBottomWithRetries,
} from "../chat-scroll";
import {
  consumeConversationScrollIntent,
  setConversationScrollIntent,
} from "../conversation-branch-scroll";
import { SessionConversationScrollRestore } from "../components/chat/SessionConversationScrollRestore";

const agentState = {
  messages: [] as Array<{ id: string; role: string }>,
  threadId: "thread-a",
};

const restoreGate = {
  restoringThreadIds: new Set<string>(),
  restoredThreadIds: new Set<string>(),
  isThreadRestoring: (threadId: string | null | undefined) =>
    Boolean(threadId && restoreGate.restoringThreadIds.has(threadId)),
  isThreadRestored: (threadId: string | null | undefined) =>
    Boolean(threadId && restoreGate.restoredThreadIds.has(threadId)),
  setThreadRestoring: vi.fn((threadId: string, restoring: boolean) => {
    if (restoring) {
      restoreGate.restoringThreadIds.add(threadId);
    } else {
      restoreGate.restoringThreadIds.delete(threadId);
    }
  }),
  markThreadRestored: vi.fn((threadId: string) => {
    restoreGate.restoredThreadIds.add(threadId);
  }),
};

vi.mock("@copilotkit/react-core/v2", () => ({
  useAgent: () => ({ agent: agentState }),
  useCopilotChatConfiguration: () => ({
    threadId: agentState.threadId,
    agentId: "dataFoundry",
  }),
}));

vi.mock("../use-data-foundry-run", () => ({
  useConversationRestoreGate: () => restoreGate,
}));

function flushEffects() {
  return act(async () => {
    await Promise.resolve();
  });
}

function mountScrollFixture(scrollTop = 0) {
  const scroll = document.createElement("div");
  scroll.style.overflowY = "auto";
  Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 300 });
  Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1200 });
  scroll.scrollTop = scrollTop;
  scroll.scrollTo = ((options?: ScrollToOptions | number) => {
    if (typeof options === "number") {
      scroll.scrollTop = options;
      return;
    }
    if (options && typeof options.top === "number") {
      scroll.scrollTop = options.top;
    }
  }) as typeof scroll.scrollTo;

  const content = document.createElement("div");
  content.setAttribute("data-testid", "copilot-scroll-content");
  scroll.appendChild(content);
  document.body.appendChild(scroll);
  return scroll;
}

describe("SessionConversationScrollRestore branch switch regression", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    document.body.innerHTML = "";
    agentState.messages = [{ id: "m1", role: "user" }];
    agentState.threadId = "thread-a";
    restoreGate.restoringThreadIds.clear();
    restoreGate.restoredThreadIds.clear();
    setConversationScrollIntent({ kind: "bottom" });
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(async () => {
    await act(async () => {
      root?.unmount();
    });
    root = null;
    host = null;
    document.body.innerHTML = "";
    vi.clearAllMocks();
  });

  it("preserves scrollTop when switching branches instead of pinning to bottom", async () => {
    const scroll = mountScrollFixture(240);
    expect(findCopilotChatScrollContainer()?.scrollTop).toBe(240);

    setConversationScrollIntent({ kind: "preserve", scrollTop: 240 });
    agentState.threadId = "thread-b";
    restoreGate.restoringThreadIds.add("thread-b");

    await act(async () => {
      root!.render(createElement(SessionConversationScrollRestore, { agentId: "dataFoundry" }));
    });

    // Still restoring: must not jump yet.
    expect(scroll.scrollTop).toBe(240);

    restoreGate.restoringThreadIds.clear();
    agentState.messages = [
      { id: "m1", role: "user" },
      { id: "m2", role: "assistant" },
    ];

    await act(async () => {
      root!.render(createElement(SessionConversationScrollRestore, { agentId: "dataFoundry" }));
    });
    await flushEffects();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 120));
    });

    expect(scroll.scrollTop).toBe(240);
    expect(scroll.scrollTop).not.toBe(scroll.scrollHeight);
  });

  it("still pins to bottom for normal session switches", async () => {
    const scroll = mountScrollFixture(80);
    setConversationScrollIntent({ kind: "bottom" });
    agentState.threadId = "thread-c";
    restoreGate.restoringThreadIds.add("thread-c");

    await act(async () => {
      root!.render(createElement(SessionConversationScrollRestore, { agentId: "dataFoundry" }));
    });

    restoreGate.restoringThreadIds.clear();
    await act(async () => {
      root!.render(createElement(SessionConversationScrollRestore, { agentId: "dataFoundry" }));
    });
    await flushEffects();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 120));
    });

    expect(scroll.scrollTop).toBe(scroll.scrollHeight);
  });

  it("captures preserve intent before the active session id changes", () => {
    const scroll = mountScrollFixture(180);
    const capturedTop = findCopilotChatScrollContainer()?.scrollTop ?? 0;
    setConversationScrollIntent({ kind: "preserve", scrollTop: capturedTop });
    expect(consumeConversationScrollIntent()).toEqual({ kind: "preserve", scrollTop: 180 });
    expect(scroll.scrollTop).toBe(180);
  });
});

describe("branch switch scroll helpers", () => {
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("restore retries keep the preserved offset while content settles", async () => {
    const scroll = mountScrollFixture(0);
    const cancel = restoreCopilotChatScrollTopWithRetries({
      scrollTop: 320,
      attempts: 3,
      intervalMs: 5,
      schedule: (callback) => {
        callback();
      },
      delay: (callback) => {
        callback();
        return 1;
      },
    });
    expect(scroll.scrollTop).toBe(320);
    cancel();
  });

  it("bottom retries still reach the end", async () => {
    const scroll = mountScrollFixture(10);
    const cancel = scrollCopilotChatToBottomWithRetries({
      attempts: 2,
      intervalMs: 5,
      schedule: (callback) => {
        callback();
      },
      delay: (callback) => {
        callback();
        return 1;
      },
    });
    expect(scroll.scrollTop).toBe(scroll.scrollHeight);
    cancel();
  });
});
