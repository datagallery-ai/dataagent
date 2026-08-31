import { afterEach, describe, expect, it, vi } from "vitest";
import {
  collectUntestedAutoTestItems,
  runAutoTestsWithConcurrency,
  scheduleDeferredWork,
} from "../workspace-config-bootstrap";
import { defaultWorkspaceConfig } from "../data-task-state";

function item(id: string, status: "connected" | "failed" | "untested") {
  return { id, name: id, description: "", enabled: true, status };
}

describe("workspace config bootstrap helpers", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("collects only untested db/llm/mcp items not already probed", () => {
    const store = defaultWorkspaceConfig();
    store.db = [item("db-1", "untested"), item("db-2", "connected")];
    store.llm = [item("llm-1", "untested"), item("llm-2", "failed")];
    store.mcp = [item("mcp-1", "untested")];
    store.kb = [item("kb-1", "untested")];
    store.skill = [item("skill-1", "untested")];

    const pending = collectUntestedAutoTestItems(store, new Set(["llm:llm-1"]));
    expect(pending.map((entry) => entry.key)).toEqual([
      "db:db-1",
      "mcp:mcp-1",
    ]);
  });

  it("runs auto-tests with bounded concurrency instead of pure serial awaits", async () => {
    const active: number[] = [];
    let maxActive = 0;
    const order: string[] = [];

    const results = await runAutoTestsWithConcurrency(
      [
        { kind: "llm", id: "a", key: "llm:a" },
        { kind: "llm", id: "b", key: "llm:b" },
        { kind: "db", id: "c", key: "db:c" },
      ],
      async (entry) => {
        active.push(1);
        maxActive = Math.max(maxActive, active.length);
        order.push(entry.key);
        await new Promise((resolve) => setTimeout(resolve, 40));
        active.pop();
        return entry.key;
      },
      2,
    );

    expect(results).toEqual(["llm:a", "llm:b", "db:c"]);
    expect(maxActive).toBe(2);
    expect(order).toEqual(["llm:a", "llm:b", "db:c"]);
  });

  it("defers work via requestIdleCallback when available", () => {
    const calls: Array<{ timeout: number }> = [];
    const cancel = vi.fn();
    vi.stubGlobal(
      "requestIdleCallback",
      (cb: IdleRequestCallback, opts?: IdleRequestOptions) => {
        calls.push({ timeout: opts?.timeout ?? -1 });
        cb({ didTimeout: false, timeRemaining: () => 10 } as IdleDeadline);
        return 7;
      },
    );
    vi.stubGlobal("cancelIdleCallback", cancel);

    const work = vi.fn();
    const dispose = scheduleDeferredWork(work, { timeoutMs: 1200 });
    expect(calls).toEqual([{ timeout: 1200 }]);
    expect(work).toHaveBeenCalledOnce();
    dispose();
    expect(cancel).toHaveBeenCalledWith(7);
  });

  it("falls back to setTimeout when requestIdleCallback is missing", () => {
    vi.useFakeTimers();
    vi.stubGlobal("requestIdleCallback", undefined);
    const work = vi.fn();
    const dispose = scheduleDeferredWork(work, { timeoutMs: 1500 });
    expect(work).not.toHaveBeenCalled();
    vi.advanceTimersByTime(249);
    expect(work).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(work).toHaveBeenCalledOnce();
    dispose();
  });
});
