import { afterEach, describe, expect, it, vi } from "vitest";
import {
  applyAutoTitle,
  createChatSession,
  dedupeChatSessions,
  deleteChatSession,
  deriveSnippetTitle,
  emptyPerRunSelection,
  getSessionDisabled,
  loadActiveLlmId,
  loadChatSessions,
  mergeServerChatSessions,
  persistActiveLlmId,
  persistChatSessions,
  prunePerRunSelection,
  renameChatSession,
  serverSessionDtoToChatSession,
  sessionEnabledIds,
  sessionResourceCounts,
  sortChatSessions,
  togglePinChatSession,
  togglePerRunMention,
  toggleSessionResource,
  isSessionResourceKindLocked,
  isSessionStarted,
  SESSION_LOCKABLE_RESOURCE_KINDS,
  type WorkspaceConfigStore,
} from "../data-task-state";

function item(
  id: string,
  name = id,
  status: "connected" | "failed" | "untested" = "connected",
) {
  return { id, name, description: `${name} desc`, enabled: true, status };
}

const store: WorkspaceConfigStore = {
  db: [item("db-default"), item("db-orders")],
  kb: [item("kb-docs")],
  mcp: [item("mcp-fs")],
  skill: [item("skill-a"), item("skill-b")],
  llm: [item("llm-1")],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("session config disabled map", () => {
  it("defaults to all enabled when config is omitted", () => {
    const session = createChatSession();
    expect(getSessionDisabled(session)).toEqual({
      db: [],
      kb: [],
      mcp: [],
      skill: [],
    });
    expect(sessionEnabledIds(store, "db", session)).toEqual([
      "db-default",
      "db-orders",
    ]);
  });

  it("toggleSessionResource adds and removes disabled ids idempotently", () => {
    let session = createChatSession();
    session = toggleSessionResource(session, "db", "db-orders");
    expect(getSessionDisabled(session).db).toEqual(["db-orders"]);
    expect(sessionEnabledIds(store, "db", session)).toEqual(["db-default"]);

    session = toggleSessionResource(session, "db", "db-orders");
    expect(getSessionDisabled(session).db).toEqual([]);
    expect(sessionEnabledIds(store, "db", session)).toEqual([
      "db-default",
      "db-orders",
    ]);
  });

  it("locks db and kb after the session has started", () => {
    const fresh = createChatSession();
    expect(SESSION_LOCKABLE_RESOURCE_KINDS).toEqual(["db", "kb"]);
    expect(isSessionStarted(fresh)).toBe(false);
    expect(isSessionResourceKindLocked(fresh, "db")).toBe(false);
    expect(isSessionResourceKindLocked(fresh, "kb")).toBe(false);
    expect(isSessionResourceKindLocked(fresh, "mcp")).toBe(false);

    const started = {
      ...fresh,
      lastMessageAt: Date.now(),
    };
    expect(isSessionStarted(started)).toBe(true);
    expect(isSessionResourceKindLocked(started, "db")).toBe(true);
    expect(isSessionResourceKindLocked(started, "kb")).toBe(true);
    expect(isSessionResourceKindLocked(started, "mcp")).toBe(false);
    expect(isSessionResourceKindLocked(started, "skill")).toBe(false);
  });

  it("treats run and message hints as session started", () => {
    const fresh = createChatSession();
    expect(isSessionStarted(fresh, { runCount: 1 })).toBe(true);
    expect(isSessionStarted(fresh, { messageCount: 2 })).toBe(true);
    expect(isSessionStarted(fresh, { hasRunHistory: true })).toBe(true);
  });

  it("sessionResourceCounts reflects enabled vs total", () => {
    let session = createChatSession();
    expect(sessionResourceCounts(store, "db", session)).toEqual({
      enabled: 2,
      total: 2,
    });
    session = toggleSessionResource(session, "db", "db-default");
    expect(sessionResourceCounts(store, "db", session)).toEqual({
      enabled: 1,
      total: 2,
    });
  });

  it("prunePerRunSelection drops mentions disabled in session", () => {
    let session = createChatSession();
    session = toggleSessionResource(session, "db", "db-orders");
    let selection = emptyPerRunSelection();
    selection = togglePerRunMention(selection, "db", "db-orders");
    selection = togglePerRunMention(selection, "db", "db-default");
    const pruned = prunePerRunSelection(store, session, selection);
    expect(pruned.db).toEqual(["db-default"]);
  });

  it("deduplicates stored sessions by session and thread id", () => {
    const first = { ...createChatSession("A"), id: "same", threadId: "same" };
    const duplicateId = { ...createChatSession("B"), id: "same", threadId: "thread-b" };
    const duplicateThread = { ...createChatSession("C"), id: "id-c", threadId: "same" };
    const unique = { ...createChatSession("D"), id: "id-d", threadId: "thread-d" };

    expect(dedupeChatSessions([first, duplicateId, duplicateThread, unique])).toEqual([
      first,
      unique,
    ]);
  });

  it("tracks manual session renames as user-authored titles", () => {
    const session = createChatSession();
    const renamed = renameChatSession([session], session.id, "订单分析")[0];

    expect(renamed.title).toBe("订单分析");
    expect(renamed.titleSource).toBe("user");
  });

  it("maps server session list DTOs into chat sessions", () => {
    const session = serverSessionDtoToChatSession({
      id: "thread-1",
      threadId: "thread-1",
      title: "渠道订单分析",
      titleSource: "llm",
      createdAt: "2026-06-27T10:00:00.000Z",
      updatedAt: "2026-06-27T10:05:00.000Z",
      lastMessageAt: "2026-06-27T10:04:00.000Z",
    });

    expect(session).toMatchObject({
      id: "thread-1",
      threadId: "thread-1",
      title: "渠道订单分析",
      titleSource: "llm",
    });
    expect(session.createdAt).toBe(new Date("2026-06-27T10:00:00.000Z").getTime());
    expect(session.updatedAt).toBe(new Date("2026-06-27T10:05:00.000Z").getTime());
    expect(session.lastMessageAt).toBe(new Date("2026-06-27T10:04:00.000Z").getTime());
  });

  it("merges server sessions with local pinned metadata", () => {
    const local = {
      ...createChatSession("本地标题"),
      id: "thread-1",
      threadId: "thread-1",
      pinned: true,
      pinnedAt: 100,
    };

    const merged = mergeServerChatSessions([local], [
      {
        id: "thread-1",
        threadId: "thread-1",
        title: "服务端标题",
        titleSource: "llm",
        updatedAt: "2026-06-27T10:05:00.000Z",
      },
    ]);

    expect(merged[0]).toMatchObject({
      id: "thread-1",
      title: "服务端标题",
      titleSource: "llm",
      pinned: true,
      pinnedAt: 100,
    });
  });

  it("lets llm titles replace default or snippet titles but not user titles", () => {
    const session = createChatSession();
    const snippet = applyAutoTitle([session], session.id, "订单趋势", "auto-snippet")[0];
    expect(snippet.title).toBe("订单趋势");
    expect(snippet.titleSource).toBe("auto-snippet");

    const llm = applyAutoTitle([snippet], session.id, "渠道订单分析", "llm")[0];
    expect(llm.title).toBe("渠道订单分析");
    expect(llm.titleSource).toBe("llm");

    const user = renameChatSession([llm], session.id, "我的复盘")[0];
    const unchanged = applyAutoTitle([user], session.id, "模型新标题", "llm")[0];
    expect(unchanged.title).toBe("我的复盘");
    expect(unchanged.titleSource).toBe("user");
  });

  it("derives compact snippet titles from the first user question", () => {
    expect(deriveSnippetTitle("  帮我分析一下\n最近 30 天不同渠道的订单走势和异常原因  ")).toBe(
      "帮我分析一下 最近 30 天不同渠道的订单走势…",
    );
    expect(deriveSnippetTitle("")).toBe("New data task");
  });

  it("stores sessions and active llm under the current user scope", () => {
    const storage = new Map<string, string>();
    vi.stubGlobal("window", {
      localStorage: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => {
          storage.set(key, value);
        },
      },
    });
    const aliceSession = { ...createChatSession("Alice task"), id: "alice-session", threadId: "alice-thread" };
    const bobSession = { ...createChatSession("Bob task"), id: "bob-session", threadId: "bob-thread" };

    persistChatSessions([aliceSession], "alice");
    persistChatSessions([bobSession], "bob");
    const llmStore = {
      ...store,
      llm: [item("alice-llm"), item("bob-llm")],
    };

    persistActiveLlmId("alice-llm", "alice");
    persistActiveLlmId("bob-llm", "bob");

    expect(loadChatSessions("alice").map((session) => session.id)).toEqual(["alice-session"]);
    expect(loadChatSessions("bob").map((session) => session.id)).toEqual(["bob-session"]);
    expect(loadActiveLlmId(llmStore, "alice")).toBe("alice-llm");
    expect(loadActiveLlmId(llmStore, "bob")).toBe("bob-llm");
  });

  it("deletes sessions and keeps pinned sessions ahead of others", () => {
    const first = { ...createChatSession("A"), pinned: true };
    const second = createChatSession("B");
    const third = createChatSession("C");

    expect(deleteChatSession([first, second, third], second.id)).toEqual([
      first,
      third,
    ]);
    expect(togglePinChatSession([first, second], second.id)[1]?.pinned).toBe(true);
    expect(togglePinChatSession([first, second], second.id)[1]?.pinnedAt).toBeTypeOf(
      "number",
    );
    expect(sortChatSessions([second, first]).map((session) => session.title)).toEqual([
      "A",
      "B",
    ]);
    const pinnedFirst = {
      ...createChatSession("Pinned first"),
      pinned: true,
      pinnedAt: 100,
    };
    const pinnedSecond = {
      ...createChatSession("Pinned second"),
      pinned: true,
      pinnedAt: 200,
    };
    expect(
      sortChatSessions([pinnedFirst, pinnedSecond]).map((session) => session.title),
    ).toEqual(["Pinned second", "Pinned first"]);
  });
});
