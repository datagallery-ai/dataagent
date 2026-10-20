import { describe, expect, it } from "vitest";

import {
  createChatSession,
  mergedSessionTitle,
  pickActiveSessionId,
} from "../data-task-state";

describe("pickActiveSessionId", () => {
  const sessions = [{ id: "first" }, { id: "current" }, { id: "other" }];

  it("keeps the current session when it still exists", () => {
    expect(pickActiveSessionId(sessions, "other", "current")).toBe("current");
  });

  it("restores the stored session after refresh when current is empty", () => {
    expect(pickActiveSessionId(sessions, "other", null)).toBe("other");
  });

  it("falls back to the first session when the stored id is gone", () => {
    expect(pickActiveSessionId(sessions, "missing", null)).toBe("first");
  });
});

describe("mergedSessionTitle", () => {
  it("keeps a user title when the server title is only a placeholder", () => {
    const local = { ...createChatSession("mr1 hello"), titleSource: "auto-snippet" as const };
    const server = { ...createChatSession("新数据任务"), titleSource: "default" as const };
    expect(mergedSessionTitle(local, server)).toEqual({
      title: "mr1 hello",
      titleSource: "auto-snippet",
    });
  });

  it("keeps a user-authored title over a generated server title", () => {
    const local = { ...createChatSession("my title"), titleSource: "user" as const };
    const server = { ...createChatSession("model title"), titleSource: "llm" as const };
    expect(mergedSessionTitle(local, server).title).toBe("my title");
  });
});
