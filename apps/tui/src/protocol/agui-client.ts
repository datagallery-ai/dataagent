import { EventSchemas, type BaseEvent } from "@ag-ui/core";
import type { AgentClient, RunAgentInput } from "./types.js";

export class AguiClientError extends Error {
  constructor(message: string, public code: string, public statusCode?: number) {
    super(message);
    this.name = "AguiClientError";
  }
}

/** Direct AG-UI transport. No CopilotKit envelope, authentication UI or run replay. */
export class AguiClient implements AgentClient {
  private controllers = new Set<AbortController>();

  constructor(
    private runtimeUrl: string,
    private fetchImpl: typeof fetch,
    private timeoutMs = 190_000,
  ) {}

  dispose(): void {
    for (const controller of this.controllers) controller.abort();
    this.controllers.clear();
  }

  async *runAgent(input: RunAgentInput): AsyncGenerator<BaseEvent> {
    const user = input.messages.filter((message) => message.role === "user").at(-1);
    if (!user) throw new AguiClientError("A new user message is required", "VALIDATION_ERROR");
    const controller = new AbortController();
    this.controllers.add(controller);
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, this.timeoutMs);
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      const response = await this.fetchImpl(this.runtimeUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({
          threadId: input.threadId, runId: input.runId, messages: [user],
          tools: [], context: [], state: {}, forwardedProps: {},
        }),
        signal: controller.signal,
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({})) as { detail?: unknown };
        throw new AguiClientError(
          typeof body.detail === "string" ? body.detail : `API request failed (${response.status})`,
          "HTTP_ERROR", response.status,
        );
      }
      if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream")) {
        throw new AguiClientError("Expected an AG-UI event stream", "INVALID_CONTENT_TYPE");
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let terminal: BaseEvent | undefined;
      while (true) {
        const { done, value } = await reader.read();
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
        let separator: RegExpExecArray | null;
        while ((separator = /\r?\n\r?\n/.exec(buffer))) {
          const frame = buffer.slice(0, separator.index);
          buffer = buffer.slice(separator.index + separator[0].length);
          const data = frame.split(/\r?\n/).filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trimStart()).join("\n");
          if (!data || data === "[DONE]") continue;
          let event: BaseEvent;
          try {
            event = EventSchemas.parse(JSON.parse(data));
          } catch {
            throw new AguiClientError("Invalid AG-UI event", "STREAM_ERROR");
          }
          if (terminal) throw new AguiClientError("Events arrived after the terminal event", "STREAM_ERROR");
          if (event.type === "RUN_FINISHED" || event.type === "RUN_ERROR") terminal = event;
          else yield event;
        }
        if (done) break;
      }
      if (!terminal || buffer.trim()) {
        throw new AguiClientError("INCOMPLETE_STREAM: connection ended before a complete terminal event", "INCOMPLETE_STREAM");
      }
      // Hold the terminal until EOF so malformed trailing events cannot produce false success.
      yield terminal;
    } catch (error) {
      if (error instanceof AguiClientError) throw error;
      if (controller.signal.aborted) {
        throw new AguiClientError(timedOut ? "The run timed out before its terminal event" : "The run was cancelled",
          timedOut ? "TIMEOUT" : "CANCELLED");
      }
      throw error;
    } finally {
      clearTimeout(timer);
      controller.abort();
      await reader?.cancel().catch(() => {});
      reader?.releaseLock();
      this.controllers.delete(controller);
    }
  }
}
