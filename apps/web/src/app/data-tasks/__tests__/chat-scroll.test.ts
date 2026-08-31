import { describe, expect, it } from "vitest";
import {
  findCopilotChatScrollContainer,
  restoreCopilotChatScrollTop,
  restoreCopilotChatScrollTopWithRetries,
  scrollCopilotChatToBottom,
  scrollCopilotChatToBottomWithRetries,
} from "../chat-scroll";
import {
  consumeConversationScrollIntent,
  isChatAutoScrollLockedNone,
  peekConversationScrollIntent,
  setConversationScrollIntent,
} from "../conversation-branch-scroll";

function createScrollFixture() {
  const content = {
    dataset: { testid: "copilot-scroll-content" },
    parentElement: null as HTMLElement | null,
  } as unknown as HTMLElement;

  const scrollCalls: ScrollToOptions[] = [];
  const scroll = {
    style: { overflowY: "auto", scrollBehavior: "" },
    scrollTop: 0,
    scrollHeight: 400,
    scrollTo(options: ScrollToOptions) {
      scrollCalls.push(options);
      const { top } = options;
      this.scrollTop = top ?? 0;
    },
    parentElement: null,
  } as unknown as HTMLElement;

  content.parentElement = scroll;
  const root = {
    querySelectorAll(selector: string) {
      return selector === '[data-testid="copilot-scroll-content"]' ? [content] : [];
    },
  } as unknown as ParentNode;

  return { root, scroll, scrollCalls };
}

describe("chat scroll helpers", () => {
  it("finds the scroll container above copilot-scroll-content", () => {
    const { root, scroll } = createScrollFixture();
    expect(findCopilotChatScrollContainer(root)).toBe(scroll);
  });

  it("scrolls the chat container to the bottom", () => {
    const { root, scroll } = createScrollFixture();
    expect(scrollCopilotChatToBottom(root)).toBe(true);
    expect(scroll.scrollTop).toBe(scroll.scrollHeight);
  });

  it("uses instant scroll even when the container has smooth scroll behavior", () => {
    const { root, scroll, scrollCalls } = createScrollFixture();
    scroll.style.scrollBehavior = "smooth";

    expect(scrollCopilotChatToBottom(root)).toBe(true);
    expect(scroll.scrollTop).toBe(scroll.scrollHeight);
    expect(scrollCalls).toEqual([{ top: scroll.scrollHeight, behavior: "auto" }]);
    expect(scroll.style.scrollBehavior).toBe("smooth");
  });

  it("skips hidden chat sessions when finding the active scroll container", () => {
    const hiddenContent = {
      dataset: { testid: "copilot-scroll-content" },
      parentElement: null as HTMLElement | null,
    } as unknown as HTMLElement;
    const hiddenScroll = {
      style: { overflowY: "auto", display: "none" },
      parentElement: null,
    } as unknown as HTMLElement;
    hiddenContent.parentElement = hiddenScroll;

    const { scroll, root } = createScrollFixture();
    const scopedRoot = {
      querySelectorAll(selector: string) {
        return selector === '[data-testid="copilot-scroll-content"]'
          ? [hiddenContent, ...(root.querySelectorAll(selector) as unknown as HTMLElement[])]
          : [];
      },
    } as unknown as ParentNode;

    expect(findCopilotChatScrollContainer(scopedRoot)).toBe(scroll);
  });

  it("prefers the ancestor that actually scrolls over a non-scrolling overflow wrapper", () => {
    const content = {
      dataset: { testid: "copilot-scroll-content" },
      parentElement: null as HTMLElement | null,
    } as unknown as HTMLElement;
    const innerOverflow = {
      style: { overflowY: "auto" },
      scrollHeight: 800,
      clientHeight: 800,
      parentElement: null as HTMLElement | null,
    } as unknown as HTMLElement;
    const scrollViewport = {
      style: { overflowY: "auto" },
      scrollHeight: 800,
      clientHeight: 300,
      parentElement: null,
    } as unknown as HTMLElement;
    content.parentElement = innerOverflow;
    innerOverflow.parentElement = scrollViewport;
    const root = {
      querySelectorAll(selector: string) {
        return selector === '[data-testid="copilot-scroll-content"]' ? [content] : [];
      },
    } as unknown as ParentNode;

    expect(findCopilotChatScrollContainer(root)).toBe(scrollViewport);
  });

  it("retries scrolling until attempts are exhausted", () => {
    const { root, scroll } = createScrollFixture();
    const calls: number[] = [];

    const cancel = scrollCopilotChatToBottomWithRetries({
      root,
      attempts: 3,
      intervalMs: 10,
      schedule: (callback) => {
        callback();
        return 0;
      },
      delay: (callback, waitMs) => {
        calls.push(waitMs);
        callback();
        return calls.length;
      },
    });

    expect(calls).toEqual([10, 10]);
    expect(scroll.scrollTop).toBe(scroll.scrollHeight);
    cancel();
  });

  it("restores a preserved scrollTop after branch switching", () => {
    const { root, scroll } = createScrollFixture();
    Object.assign(scroll, {
      scrollTop: 0,
      clientHeight: 300,
      scrollHeight: 900,
    });

    expect(restoreCopilotChatScrollTop(240, root)).toBe(true);
    expect(scroll.scrollTop).toBe(240);
  });

  it("clamps preserved scrollTop to the new content height", () => {
    const { root, scroll } = createScrollFixture();
    Object.assign(scroll, {
      scrollTop: 0,
      clientHeight: 300,
      scrollHeight: 400,
    });

    expect(restoreCopilotChatScrollTop(900, root)).toBe(true);
    expect(scroll.scrollTop).toBe(100);
  });

  it("retries restoring scrollTop until attempts are exhausted", () => {
    const { root, scroll } = createScrollFixture();
    Object.assign(scroll, {
      scrollTop: 0,
      clientHeight: 300,
      scrollHeight: 900,
    });
    const calls: number[] = [];

    const cancel = restoreCopilotChatScrollTopWithRetries({
      scrollTop: 180,
      root,
      attempts: 3,
      intervalMs: 10,
      schedule: (callback) => {
        callback();
        return 0;
      },
      delay: (callback, waitMs) => {
        calls.push(waitMs);
        callback();
        return calls.length;
      },
    });

    expect(calls).toEqual([10, 10]);
    expect(scroll.scrollTop).toBe(180);
    cancel();
  });

  it("tracks preserve scroll intent and locks autoScroll off StickToBottom", () => {
    setConversationScrollIntent({ kind: "bottom" });
    expect(peekConversationScrollIntent()).toEqual({ kind: "bottom" });
    expect(isChatAutoScrollLockedNone()).toBe(false);
    setConversationScrollIntent({ kind: "preserve", scrollTop: 180 });
    expect(isChatAutoScrollLockedNone()).toBe(true);
    expect(consumeConversationScrollIntent()).toEqual({ kind: "preserve", scrollTop: 180 });
    expect(peekConversationScrollIntent()).toEqual({ kind: "bottom" });
    expect(isChatAutoScrollLockedNone()).toBe(true);
  });
});
