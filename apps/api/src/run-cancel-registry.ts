export type RunCancelHandle = {
  cancel(reason?: string): void;
  runId: string;
  sessionId: string;
  userId: string;
};

export type RunCancelResult =
  | {
      canceled: true;
      runId: string;
      sessionId: string;
    }
  | {
      canceled: false;
      reason: "not_active";
      runId: string;
    };

/** Process-local registry for active runs that can be canceled by REST control APIs. */
export class RunCancelRegistry {
  private readonly handles = new Map<string, RunCancelHandle>();

  register(input: RunCancelHandle): () => void {
    const key = this.key(input.userId, input.runId);
    this.handles.set(key, input);
    return () => {
      const current = this.handles.get(key);
      if (current === input) {
        this.handles.delete(key);
      }
    };
  }

  cancel(input: { reason?: string; runId: string; userId: string }): RunCancelResult {
    const handle = this.handles.get(this.key(input.userId, input.runId));
    if (!handle) {
      return { canceled: false, reason: "not_active", runId: input.runId };
    }
    handle.cancel(input.reason);
    return { canceled: true, runId: handle.runId, sessionId: handle.sessionId };
  }

  /** True when this process still owns a live cancel handle for the run. */
  has(input: { runId: string; userId: string }): boolean {
    return this.handles.has(this.key(input.userId, input.runId));
  }

  private key(userId: string, runId: string): string {
    return `${userId}:${runId}`;
  }
}
