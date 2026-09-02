import type { BaseEvent } from "@ag-ui/client";

import { consumeSseBuffer } from "./sse.js";
import {
  RUNTIME_CONTRACT_VERSION,
  RUNTIME_PROVIDER,
  type RuntimeHealth,
  type RuntimeRunRequest,
  type RuntimeTransport
} from "./types.js";

export type HttpRuntimeClientOptions = {
  token?: string;
  url: string;
};

const DEFAULT_HEALTH: RuntimeHealth = {
  status: "unavailable",
  provider: RUNTIME_PROVIDER,
  version: RUNTIME_CONTRACT_VERSION,
  capabilities: {
    streaming: false,
    tools: false,
    interrupt: false,
    cancel: false
  }
};

export class HttpRuntimeClient implements RuntimeTransport {
  constructor(private readonly options: HttpRuntimeClientOptions) {}

  async health(): Promise<RuntimeHealth> {
    try {
      const response = await fetch(new URL("/health", this.options.url), {
        headers: this.headers()
      });
      if (!response.ok) {
        return { ...DEFAULT_HEALTH, status: "degraded" };
      }
      const body = await response.json() as Partial<RuntimeHealth>;
      return {
        status: body.status === "ok" || body.status === "degraded" ? body.status : "degraded",
        provider: typeof body.provider === "string" ? body.provider : RUNTIME_PROVIDER,
        version: typeof body.version === "string" ? body.version : RUNTIME_CONTRACT_VERSION,
        capabilities: {
          streaming: body.capabilities?.streaming !== false,
          tools: Boolean(body.capabilities?.tools),
          interrupt: Boolean(body.capabilities?.interrupt),
          cancel: Boolean(body.capabilities?.cancel)
        }
      };
    } catch {
      return DEFAULT_HEALTH;
    }
  }

  async *startRun(
    request: RuntimeRunRequest,
    options: { signal?: AbortSignal } = {}
  ): AsyncIterable<BaseEvent> {
    const response = await fetch(new URL("/runs/stream", this.options.url), {
      method: "POST",
      headers: {
        ...this.headers(),
        Accept: "text/event-stream",
        "Content-Type": "application/json"
      },
      body: JSON.stringify(request),
      ...(options.signal ? { signal: options.signal } : {})
    });
    if (!response.ok || !response.body) {
      const detail = await response.text().catch(() => "");
      throw new Error(`RUNTIME_STREAM_FAILED:${response.status}:${detail.slice(0, 200)}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const parsed = consumeSseBuffer(buffer);
        buffer = parsed.remainder;
        for (const event of parsed.events) {
          yield event as BaseEvent;
        }
      }
      if (buffer.trim()) {
        const parsed = consumeSseBuffer(`${buffer}\n\n`);
        for (const event of parsed.events) {
          yield event as BaseEvent;
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  async cancelRun(runId: string, reason = "RUN_CANCELLED"): Promise<void> {
    const response = await fetch(new URL(`/runs/${encodeURIComponent(runId)}/cancel`, this.options.url), {
      method: "POST",
      headers: {
        ...this.headers(),
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ reason })
    });
    if (!response.ok && response.status !== 404) {
      throw new Error(`RUNTIME_CANCEL_FAILED:${response.status}`);
    }
  }

  private headers(): Record<string, string> {
    return this.options.token
      ? { Authorization: `Bearer ${this.options.token}` }
      : {};
  }
}

export const createHttpRuntimeClient = (options: HttpRuntimeClientOptions): RuntimeTransport =>
  new HttpRuntimeClient(options);
