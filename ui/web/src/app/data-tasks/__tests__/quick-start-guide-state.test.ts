import { describe, expect, it } from "vitest";
import { createTranslator } from "../../../i18n/translate";
import {
  QUICK_START_PROMPT_SEEN_STORAGE_KEY,
  QUICK_START_STEP_ORDER,
  getQuickStartPromptSeenStorageKey,
  getQuickStartInitialStep,
  hasSeenQuickStartPrompt,
  markQuickStartPromptSeen,
  resolveQuickStartReadiness,
  resolveQuickStartStep,
  type QuickStartStorage,
} from "../components/guide/quick-start-guide-state";
import type { WorkspaceConfigStore } from "../data-task-state";

const t = createTranslator("en");
const tZh = createTranslator("zh-CN");

function item(id: string, enabled = true, name = id) {
  return { id, name, description: `${name} description`, enabled };
}

function workspaceConfig(
  overrides: Partial<WorkspaceConfigStore> = {},
): WorkspaceConfigStore {
  return {
    db: [],
    kb: [],
    mcp: [],
    skill: [],
    llm: [],
    ...overrides,
  };
}

function memoryStorage(initial: Record<string, string> = {}): QuickStartStorage {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
}

describe("quick start guide state", () => {
  it("persists whether the first quick-start prompt has been seen", () => {
    const storage = memoryStorage();

    expect(hasSeenQuickStartPrompt(storage)).toBe(false);

    markQuickStartPromptSeen(storage);

    expect(hasSeenQuickStartPrompt(storage)).toBe(true);
  });

  it("tracks the first quick-start prompt per user", () => {
    const storage = memoryStorage();

    expect(getQuickStartPromptSeenStorageKey()).toBe(
      QUICK_START_PROMPT_SEEN_STORAGE_KEY,
    );
    expect(hasSeenQuickStartPrompt(storage, "user-a")).toBe(false);
    expect(hasSeenQuickStartPrompt(storage, "user-b")).toBe(false);

    markQuickStartPromptSeen(storage, "user-a");

    expect(hasSeenQuickStartPrompt(storage, "user-a")).toBe(true);
    expect(hasSeenQuickStartPrompt(storage, "user-b")).toBe(false);
  });

  it("prefers the first available datasource when resolving readiness", () => {
    const config = workspaceConfig({
      llm: [item("server-default", true, "Server default")],
      db: [
        item("warehouse", true, "Warehouse"),
        item("api-duckdb-demo", true, "API DuckDB Demo"),
      ],
    });

    expect(resolveQuickStartReadiness(config)).toEqual({
      hasModel: true,
      hasDatasource: true,
      preferredDatasourceId: "warehouse",
      canRun: true,
    });
  });

  it("reports no preferred datasource when none are configured", () => {
    expect(resolveQuickStartReadiness(workspaceConfig({ llm: [item("llm")] }))).toEqual({
      hasModel: true,
      hasDatasource: false,
      preferredDatasourceId: null,
      canRun: false,
    });
  });

  it("treats configured llm profiles as runnable even when legacy enabled is false", () => {
    const config = workspaceConfig({
      llm: [item("server-default", false, "Server default")],
      db: [item("api-duckdb-demo")],
    });

    expect(resolveQuickStartReadiness(config).hasModel).toBe(true);
    expect(resolveQuickStartReadiness(config).canRun).toBe(true);
  });

  it("starts by orienting users to the left, middle, and right work areas", () => {
    expect(QUICK_START_STEP_ORDER).toEqual([
      "welcome",
      "query",
      "send",
      "console",
      "output",
    ]);
    expect(getQuickStartInitialStep(resolveQuickStartReadiness(workspaceConfig()))).toBe(
      "welcome",
    );
    expect(
      getQuickStartInitialStep(
        resolveQuickStartReadiness(workspaceConfig({ llm: [item("llm")] })),
      ),
    ).toBe("welcome");
    expect(
      getQuickStartInitialStep(
        resolveQuickStartReadiness(
          workspaceConfig({ llm: [item("llm")], db: [item("api-duckdb-demo")] }),
        ),
      ),
    ).toBe("welcome");
  });

  it("returns actionable copy and anchors for each quick-start step", () => {
    const welcome = resolveQuickStartStep("welcome", {
      readiness: resolveQuickStartReadiness(workspaceConfig()),
      runStatus: "idle",
      t,
    });
    const query = resolveQuickStartStep("query", {
      readiness: resolveQuickStartReadiness(
        workspaceConfig({ llm: [item("llm")], db: [item("api-duckdb-demo")] }),
      ),
      runStatus: "idle",
      t,
    });
    const output = resolveQuickStartStep("output", {
      readiness: resolveQuickStartReadiness(
        workspaceConfig({ llm: [item("llm")], db: [item("api-duckdb-demo")] }),
      ),
      runStatus: "completed",
      hasSubmittedTask: true,
      t,
    });

    expect(welcome.targetId).toBe("workspace-layout");
    expect(welcome.body).toContain("Left");
    expect(welcome.body).toContain("middle");
    expect(welcome.body).toContain("right");
    expect(welcome.body).toContain("conversations");
    expect(query.targetId).toBe("chat-input");
    expect(query.body).toContain(t("welcome.runSqlPrompt"));
    expect(query.cta).toBe("Use this query");
    expect(output.targetId).toBe("run-console");
    expect(output.title).toBe("Review the result");
  });

  it("keeps the send step blocked until the user submits the task", () => {
    const readiness = resolveQuickStartReadiness(
      workspaceConfig({ llm: [item("llm")], db: [item("api-duckdb-demo")] }),
    );

    const waiting = resolveQuickStartStep("send", {
      readiness,
      runStatus: "idle",
      hasSubmittedTask: false,
      t,
    });
    const submitted = resolveQuickStartStep("send", {
      readiness,
      runStatus: "running",
      hasSubmittedTask: true,
      t,
    });

    expect(waiting.cta).toBe("Waiting for send");
    expect(waiting.blocked).toBe(true);
    expect(waiting.body).toContain("send arrow");
    expect(submitted.cta).toBe("Next");
    expect(submitted.blocked).toBe(false);
  });

  it("resolves Chinese shell copy for the welcome step", () => {
    const welcome = resolveQuickStartStep("welcome", {
      readiness: resolveQuickStartReadiness(workspaceConfig()),
      runStatus: "idle",
      t: tZh,
    });

    expect(welcome.title).toBe("先看清工作区布局");
    expect(welcome.body).toContain("左侧");
    expect(welcome.cta).toBe("下一步");
  });

  it("localizes the sample query prompt for Chinese", () => {
    const query = resolveQuickStartStep("query", {
      readiness: resolveQuickStartReadiness(
        workspaceConfig({ llm: [item("llm")], db: [item("api-duckdb-demo")] }),
      ),
      runStatus: "idle",
      t: tZh,
    });

    expect(query.body).toContain("查询最近 30 天的订单总量，按日期分组");
    expect(query.cta).toBe("使用此查询");
  });
});
