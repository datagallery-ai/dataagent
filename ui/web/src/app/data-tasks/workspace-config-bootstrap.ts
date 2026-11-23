import type { WorkspaceConfigKind, WorkspaceConfigStore } from "./data-task-state";

export const AUTO_TEST_KINDS: WorkspaceConfigKind[] = ["db", "llm", "mcp"];

export type AutoTestEntry = {
  kind: WorkspaceConfigKind;
  id: string;
  key: string;
};

/** Collect untested db/llm/mcp items that have not been probed this session. */
export function collectUntestedAutoTestItems(
  workspaceConfig: WorkspaceConfigStore,
  alreadyTested: ReadonlySet<string>,
): AutoTestEntry[] {
  const pending: AutoTestEntry[] = [];
  for (const kind of AUTO_TEST_KINDS) {
    for (const item of workspaceConfig[kind]) {
      const key = `${kind}:${item.id}`;
      if (item.status === "untested" && !alreadyTested.has(key)) {
        pending.push({ kind, id: item.id, key });
      }
    }
  }
  return pending;
}

/**
 * Run connectivity probes with bounded concurrency so startup is not blocked
 * by a long serial chain of LLM/MCP/DB tests.
 */
export async function runAutoTestsWithConcurrency<T>(
  entries: readonly AutoTestEntry[],
  runOne: (entry: AutoTestEntry) => Promise<T>,
  concurrency = 2,
): Promise<T[]> {
  if (entries.length === 0) return [];
  const limit = Math.max(1, Math.min(concurrency, entries.length));
  const results: T[] = new Array(entries.length);
  let nextIndex = 0;

  async function worker(): Promise<void> {
    while (nextIndex < entries.length) {
      const index = nextIndex;
      nextIndex += 1;
      results[index] = await runOne(entries[index]!);
    }
  }

  await Promise.all(Array.from({ length: limit }, () => worker()));
  return results;
}

/**
 * Defer work until the browser is idle (or a short timeout) so first paint /
 * chat shell can become interactive before connectivity probes start.
 */
export function scheduleDeferredWork(
  work: () => void,
  options?: { timeoutMs?: number },
): () => void {
  const timeoutMs = options?.timeoutMs ?? 1500;
  const scheduleIdle = (
    globalThis as typeof globalThis & {
      requestIdleCallback?: (cb: IdleRequestCallback, opts?: IdleRequestOptions) => number;
      cancelIdleCallback?: (id: number) => void;
    }
  ).requestIdleCallback;
  const cancelIdle = (
    globalThis as typeof globalThis & {
      cancelIdleCallback?: (id: number) => void;
    }
  ).cancelIdleCallback;

  if (typeof scheduleIdle === "function") {
    const idleId = scheduleIdle(() => work(), { timeout: timeoutMs });
    return () => {
      if (typeof cancelIdle === "function") cancelIdle(idleId);
    };
  }
  const timer = setTimeout(work, Math.min(timeoutMs, 250));
  return () => clearTimeout(timer);
}
