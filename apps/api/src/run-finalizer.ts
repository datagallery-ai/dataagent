import { EventType, type BaseEvent } from "@ag-ui/client";
import {
  createCustomEvent,
  type GoalRuntimeAdapter,
  type ProtocolCompletionDecision
} from "@datafoundry/agent-runtime";
import type { FileAssetService } from "@datafoundry/files";
import { readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import type { MetadataStore } from "@datafoundry/metadata";

export type RunStatus = "running" | "suspended" | "completed" | "failed" | "canceled";

type RunFinalizerInput = {
  destroyWorkspace(): Promise<void>;
  emit(event: BaseEvent): void;
  fileAssetService: FileAssetService;
  flushCompletedMemory(input: { emit(event: BaseEvent): void; signal: AbortSignal }): Promise<void>;
  flushDraftsMemory?(): void;
  memoryExtractionTimeoutMs: number;
  metadataStore: MetadataStore;
  runId: string;
  /** Per-session directory; agent outputs here are synced to session-scoped file_asset_refs. */
  sessionDir: string;
  sessionId: string;
  userId: string;
  workspaceId: string;
};

/** Owns terminal run state transitions and their externally visible AG-UI event order. */
export class RunFinalizer {
  private readonly input: RunFinalizerInput;

  constructor(input: RunFinalizerInput) {
    this.input = input;
  }

  suspend(): void {
    this.input.flushDraftsMemory?.();
    this.input.metadataStore.runs.updateStatus({
      user_id: this.input.userId,
      run_id: this.input.runId,
      status: "suspended"
    });
    this.input.emit(createRunStatusDelta("suspended", { runId: this.input.runId }));
  }

  async cancel(input: { interactionResolvedEvent: BaseEvent; terminalEvent: BaseEvent }): Promise<void> {
    this.input.emit(input.interactionResolvedEvent);
    this.input.metadataStore.runs.updateStatus({
      user_id: this.input.userId,
      run_id: this.input.runId,
      status: "canceled"
    });
    this.input.emit(createRunStatusDelta("canceled", { runId: this.input.runId }));
    await this.input.destroyWorkspace().catch(() => undefined);
    this.input.emit(input.terminalEvent);
  }

  async cancelRun(input: { reason?: string | undefined; terminalEvent: BaseEvent }): Promise<void> {
    this.input.flushDraftsMemory?.();
    await this.syncSessionOutputs().catch(() => undefined);
    this.input.metadataStore.runs.updateStatus({
      user_id: this.input.userId,
      run_id: this.input.runId,
      status: "canceled",
      ...(input.reason ? { error_message: input.reason } : {})
    });
    this.input.emit(createRunStatusDelta("canceled", {
      errorMessage: input.reason,
      runId: this.input.runId
    }));
    await this.input.destroyWorkspace().catch(() => undefined);
    this.input.emit(input.terminalEvent);
  }

  async complete(input: {
    goalRuntime?: GoalRuntimeAdapter | undefined;
    terminalDecision?: ProtocolCompletionDecision | undefined;
    terminalEvent: BaseEvent;
  }): Promise<void> {
    if (!input.terminalDecision || input.terminalDecision.status === "continue") {
      throw new Error("PROTOCOL_TERMINAL_DECISION_REQUIRED");
    }
    if (input.terminalDecision.status === "failed") {
      throw new Error("PROTOCOL_FAILED_DECISION_REQUIRES_FAIL_FINALIZATION");
    }
    if (input.goalRuntime) {
      this.input.emit(createCustomEvent("goal.updated", {
        goal: await input.goalRuntime.getSnapshot(),
        source: "mastra-native-goal"
      }));
    }
    await this.flushCompletedMemoryWithTimeout();
    // Sync durable agent outputs (write_file / execute_command / edit_file / mkdir, etc.)
    // into the asset store so they get a file_id and become @-referenceable / restorable.
    // Scans the per-session directory and registers session-scoped refs (session_id set)
    // so the frontend can restore this session's files (R-023/R-025). The cross-session
    // workspace root is NOT scanned here — promoted files already have workspace refs.
    // Best-effort: never blocks run completion on failure.
    await this.syncSessionOutputs().catch(() => undefined);
    this.input.metadataStore.runs.updateStatus({
      user_id: this.input.userId,
      run_id: this.input.runId,
      status: "completed"
    });
    this.input.emit(createRunStatusDelta("completed", { runId: this.input.runId }));
    await this.input.destroyWorkspace().catch(() => undefined);
    this.input.emit(input.terminalEvent);
  }

  fail(input: { errorMessage: string; terminalEvent: BaseEvent }): void {
    this.input.flushDraftsMemory?.();
    this.input.metadataStore.runs.updateStatus({
      user_id: this.input.userId,
      run_id: this.input.runId,
      status: "failed",
      error_message: input.errorMessage
    });
    this.input.emit(createRunStatusDelta("failed", {
      errorMessage: input.errorMessage,
      runId: this.input.runId
    }));
    void this.input.destroyWorkspace().catch(() => undefined);
    this.input.emit(input.terminalEvent);
  }

  /**
   * Walk the per-session directory and ensure every durable file has a file_id via
   * syncWorkspaceFile (session-scoped ref). Then GC orphaned assets left by content
   * edits (reassignAsset orphans the previous asset). The session directory remains the
   * active working copy; refs point into the content-addressed asset store, so files that
   * were synced here remain downloadable / restorable by the frontend.
   */
  private async syncSessionOutputs(): Promise<void> {
    const sessionDir = this.input.sessionDir;
    const walk = (dir: string): string[] => {
      let entries: string[];
      try {
        entries = readdirSync(dir);
      } catch {
        return [];
      }
      const files: string[] = [];
      for (const entry of entries) {
        const full = join(dir, entry);
        let st;
        try {
          st = statSync(full);
        } catch {
          continue;
        }
        if (st.isDirectory()) {
          files.push(...walk(full));
        } else if (st.isFile()) {
          files.push(full);
        }
      }
      return files;
    };

    for (const file of walk(sessionDir)) {
      const rel = relative(sessionDir, file).split(sep).join("/");
      try {
        this.input.fileAssetService.syncWorkspaceFile({
          user_id: this.input.userId,
          workspace_id: this.input.workspaceId,
          filename: rel,
          path: file,
          session_id: this.input.sessionId,
          run_id: this.input.runId
        });
      } catch {
        // best-effort per file
      }
    }
    try {
      this.input.fileAssetService.gcOrphanAssets();
    } catch {
      // best-effort
    }
  }

  private async flushCompletedMemoryWithTimeout(): Promise<void> {
    const timeoutMs = normalizeTimeoutMs(this.input.memoryExtractionTimeoutMs);
    const controller = new AbortController();
    const memoryEmitter = createScopedEmitter(this.input.emit);
    const task = this.input.flushCompletedMemory({
      emit: memoryEmitter.emit,
      signal: controller.signal
    });
    const result = await settleWithTimeout(task, timeoutMs, () => {
      controller.abort(new Error("MEMORY_EXTRACTION_TIMEOUT"));
      memoryEmitter.close();
    });

    if (result.status === "timeout") {
      this.input.emit(createCustomEvent("memory.completed-flush.timeout", {
        source: "completed-run",
        timeout_ms: timeoutMs
      }));
      return;
    }
    if (result.status === "failed") {
      this.input.emit(createCustomEvent("memory.completed-flush.failed", {
        error_message: result.errorMessage,
        source: "completed-run"
      }));
    }
  }
}

export const createRunStatusDelta = (
  status: RunStatus,
  options: { errorMessage?: string | undefined; runId?: string | undefined } = {}
): BaseEvent => ({
  type: EventType.STATE_DELTA,
  ...(options.runId ? { runId: options.runId } : {}),
  delta: [
    { op: "add", path: "/runStatus", value: status },
    ...(options.errorMessage ? [{ op: "add", path: "/errorMessage", value: options.errorMessage }] : [])
  ],
  timestamp: Date.now()
} as BaseEvent);

const DEFAULT_MEMORY_EXTRACTION_TIMEOUT_MS = 2000;

type TimeoutResult =
  | { status: "completed" }
  | { errorMessage: string; status: "failed" }
  | { status: "timeout" };

const settleWithTimeout = async (
  task: Promise<void>,
  timeoutMs: number,
  onTimeout: () => void
): Promise<TimeoutResult> => {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const watchedTask = task.then(
    () => ({ status: "completed" as const }),
    (error: unknown) => ({ errorMessage: errorMessage(error), status: "failed" as const })
  );
  const timeout = new Promise<TimeoutResult>((resolve) => {
    timer = setTimeout(() => {
      onTimeout();
      resolve({ status: "timeout" });
    }, timeoutMs);
  });
  const result = await Promise.race([watchedTask, timeout]);
  if (timer) {
    clearTimeout(timer);
  }
  void watchedTask.catch(() => undefined);
  return result;
};

const createScopedEmitter = (emit: (event: BaseEvent) => void): {
  close(): void;
  emit(event: BaseEvent): void;
} => {
  let closed = false;
  return {
    close: () => {
      closed = true;
    },
    emit: (event) => {
      if (!closed) {
        emit(event);
      }
    }
  };
};

const normalizeTimeoutMs = (value: number): number =>
  Number.isFinite(value) && value > 0 ? value : DEFAULT_MEMORY_EXTRACTION_TIMEOUT_MS;

const errorMessage = (error: unknown): string => error instanceof Error ? error.message : "Unknown memory flush error";
