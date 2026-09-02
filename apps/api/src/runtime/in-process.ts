import type { BaseEvent } from "@ag-ui/client";

import { generateStubEvents } from "./scenarios.js";
import {
  RUNTIME_CONTRACT_VERSION,
  RUNTIME_PROVIDER,
  type RuntimeHealth,
  type RuntimeRunRequest,
  type RuntimeTransport
} from "./types.js";

const canceledRuns = new Set<string>();

export class InProcessRuntime implements RuntimeTransport {
  async health(): Promise<RuntimeHealth> {
    return {
      status: "ok",
      provider: `${RUNTIME_PROVIDER}-stub`,
      version: RUNTIME_CONTRACT_VERSION,
      capabilities: {
        streaming: true,
        tools: true,
        interrupt: true,
        cancel: true
      }
    };
  }

  async *startRun(
    request: RuntimeRunRequest,
    options: { signal?: AbortSignal } = {}
  ): AsyncIterable<BaseEvent> {
    canceledRuns.delete(request.runId);
    for (const event of generateStubEvents(request)) {
      if (options.signal?.aborted || canceledRuns.has(request.runId)) {
        return;
      }
      yield event;
    }
  }

  async cancelRun(runId: string): Promise<void> {
    canceledRuns.add(runId);
  }
}

export const createInProcessRuntime = (): RuntimeTransport => new InProcessRuntime();
