import { describe, expect, it, vi } from "vitest";

import { RunCancelRegistry } from "./run-cancel-registry.js";
import {
  reclaimOrphanedQueuedAndRunningRuns,
  resolveLiveSessionActiveRun,
} from "./stale-active-runs.js";

type FakeRun = {
  id: string;
  user_id: string;
  session_id: string;
  status: "queued" | "running" | "suspended" | "canceled" | "completed" | "failed";
  error_message?: string | null;
};

function createFakeStore(initial: FakeRun[]) {
  const runs = [...initial];
  return {
    runs: {
      findActiveBySession(input: {
        exclude_run_id?: string;
        session_id: string;
        user_id: string;
      }) {
        return (
          runs.find(
            (run) =>
              run.user_id === input.user_id &&
              run.session_id === input.session_id &&
              (run.status === "queued" ||
                run.status === "running" ||
                run.status === "suspended") &&
              run.id !== input.exclude_run_id,
          ) ?? undefined
        );
      },
      listByStatuses(input: { statuses: Array<FakeRun["status"]> }) {
        return runs.filter((run) => input.statuses.includes(run.status));
      },
      updateStatus(input: {
        user_id: string;
        run_id: string;
        status: FakeRun["status"];
        error_message?: string;
      }) {
        const run = runs.find(
          (item) => item.user_id === input.user_id && item.id === input.run_id,
        );
        if (!run) {
          throw new Error(`missing ${input.run_id}`);
        }
        run.status = input.status;
        run.error_message = input.error_message ?? null;
        return run;
      },
    },
  };
}

describe("resolveLiveSessionActiveRun", () => {
  it("reclaims orphaned queued/running rows without a live cancel handle", () => {
    const store = createFakeStore([
      {
        id: "run-zombie",
        user_id: "u1",
        session_id: "s1",
        status: "running",
      },
    ]);
    const registry = new RunCancelRegistry();

    const active = resolveLiveSessionActiveRun({
      metadataStore: store as never,
      runCancelRegistry: registry,
      userId: "u1",
      sessionId: "s1",
    });

    expect(active).toBeNull();
    expect(store.runs.findActiveBySession({ user_id: "u1", session_id: "s1" })).toBeUndefined();
  });

  it("keeps live running runs and suspended HITL locks", () => {
    const store = createFakeStore([
      {
        id: "run-live",
        user_id: "u1",
        session_id: "s1",
        status: "running",
      },
      {
        id: "run-hitl",
        user_id: "u1",
        session_id: "s2",
        status: "suspended",
      },
    ]);
    const registry = new RunCancelRegistry();
    registry.register({
      cancel: vi.fn(),
      runId: "run-live",
      sessionId: "s1",
      userId: "u1",
    });

    expect(
      resolveLiveSessionActiveRun({
        metadataStore: store as never,
        runCancelRegistry: registry,
        userId: "u1",
        sessionId: "s1",
      })?.id,
    ).toBe("run-live");

    expect(
      resolveLiveSessionActiveRun({
        metadataStore: store as never,
        runCancelRegistry: registry,
        userId: "u1",
        sessionId: "s2",
      })?.id,
    ).toBe("run-hitl");
  });
});

describe("reclaimOrphanedQueuedAndRunningRuns", () => {
  it("cancels orphaned queued/running rows on startup", () => {
    const store = createFakeStore([
      {
        id: "run-a",
        user_id: "u1",
        session_id: "s1",
        status: "queued",
      },
      {
        id: "run-b",
        user_id: "u1",
        session_id: "s2",
        status: "running",
      },
      {
        id: "run-c",
        user_id: "u1",
        session_id: "s3",
        status: "suspended",
      },
    ]);
    const registry = new RunCancelRegistry();
    registry.register({
      cancel: vi.fn(),
      runId: "run-b",
      sessionId: "s2",
      userId: "u1",
    });

    const reclaimed = reclaimOrphanedQueuedAndRunningRuns({
      metadataStore: store as never,
      runCancelRegistry: registry,
    });

    expect(reclaimed).toBe(1);
    expect(store.runs.listByStatuses({ statuses: ["queued"] })).toHaveLength(0);
    expect(store.runs.listByStatuses({ statuses: ["running"] })[0]?.id).toBe("run-b");
    expect(store.runs.listByStatuses({ statuses: ["suspended"] })[0]?.id).toBe("run-c");
  });
});
