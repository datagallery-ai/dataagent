import type { MetadataStore, RunRecord } from "@datafoundry/metadata";

import type { RunCancelRegistry } from "./run-cancel-registry.js";

const LIVE_CLAIM_STATUSES = new Set(["queued", "running"]);

/**
 * Active metadata rows can outlive the in-process worker after crashes/restarts.
 * Suspended (HITL) runs stay locked; queued/running without a cancel handle are
 * treated as orphans and canceled so a new claim can proceed.
 */
export function resolveLiveSessionActiveRun(input: {
  excludeRunId?: string;
  metadataStore: MetadataStore;
  runCancelRegistry: RunCancelRegistry;
  sessionId: string;
  userId: string;
}): RunRecord | null {
  for (;;) {
    const active = input.metadataStore.runs.findActiveBySession({
      user_id: input.userId,
      session_id: input.sessionId,
      ...(input.excludeRunId ? { exclude_run_id: input.excludeRunId } : {})
    });
    if (!active) {
      return null;
    }
    if (active.status === "suspended") {
      return active;
    }
    if (
      LIVE_CLAIM_STATUSES.has(active.status)
      && input.runCancelRegistry.has({ userId: input.userId, runId: active.id })
    ) {
      return active;
    }
    if (!LIVE_CLAIM_STATUSES.has(active.status)) {
      return active;
    }
    input.metadataStore.runs.updateStatus({
      user_id: input.userId,
      run_id: active.id,
      status: "canceled",
      error_message: "STALE_ACTIVE_RUN_RECLAIMED"
    });
  }
}

/** Cancel queued/running rows that have no live cancel handle (e.g. after process restart). */
export function reclaimOrphanedQueuedAndRunningRuns(input: {
  metadataStore: MetadataStore;
  runCancelRegistry: RunCancelRegistry;
}): number {
  const candidates = input.metadataStore.runs.listByStatuses({
    statuses: ["queued", "running"]
  });
  let reclaimed = 0;
  for (const run of candidates) {
    if (input.runCancelRegistry.has({ userId: run.user_id, runId: run.id })) {
      continue;
    }
    input.metadataStore.runs.updateStatus({
      user_id: run.user_id,
      run_id: run.id,
      status: "canceled",
      error_message: "STALE_ACTIVE_RUN_RECLAIMED"
    });
    reclaimed += 1;
  }
  return reclaimed;
}
