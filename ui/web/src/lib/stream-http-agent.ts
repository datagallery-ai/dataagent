import {
  EventSchemas,
  EventType,
  HttpAgent,
  type BaseEvent,
  type HttpAgentConfig,
  type RunAgentInput,
} from "@ag-ui/client";
import { Observable } from "rxjs";
import { reportClientError } from "./client-errors";
import {
  STREAM_RUNTIME_PATH,
  formatStreamHttpError,
  sseData,
  takeClientEvents,
  takeSseFrames,
  toStreamRunInput,
  type OpenChildStep,
} from "./stream-run";

/** CopilotKit renderer pointed at `/api/v1/runs` with the TUI request contract. */
export class StreamHttpAgent extends HttpAgent {
  constructor(config: Omit<HttpAgentConfig, "url"> = {}) {
    super({ ...config, url: STREAM_RUNTIME_PATH });
  }

  protected requestInit(input: RunAgentInput): RequestInit {
    return super.requestInit(toStreamRunInput(input));
  }

  override run(input: RunAgentInput): Observable<BaseEvent> {
    const parent = this.abortController.signal;
    return new Observable((subscriber) => {
      let init: RequestInit;
      try {
        init = this.requestInit(input);
      } catch (error) {
        reportClientError(error, { threadId: input.threadId, runId: input.runId, stage: "request" });
        subscriber.error(error instanceof Error ? error : new Error(String(error)));
        return;
      }
      const controller = new AbortController();
      const onParentAbort = () => controller.abort();
      if (parent.aborted) controller.abort();
      else parent.addEventListener("abort", onParentAbort);

      let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
      let settled = false;
      let sawTerminal = false;
      let requestId: string | undefined;
      const finish = (error?: unknown) => {
        if (settled) return;
        settled = true;
        if (error !== undefined && isAbort(error, controller)) {
          if (!sawTerminal) subscriber.next(cancelledEvent());
          subscriber.complete();
          return;
        }
        if (error !== undefined) {
          subscriber.error(error instanceof Error ? error : new Error(String(error)));
          return;
        }
        subscriber.complete();
      };

      void (async () => {
        try {
          const response = await this.fetch(this.url, { ...init, signal: controller.signal });
          requestId = response.headers.get("x-request-id") ?? undefined;
          if (!response.ok) {
            throw new Error(formatStreamHttpError(response.status, await response.text()));
          }
          const contentType = response.headers.get("content-type") ?? "";
          if (!response.body || !contentType.includes("text/event-stream")) {
            throw new Error("Expected an AG-UI event stream");
          }
          reader = response.body.getReader();
          const decoder = new TextDecoder();
          const openSteps = new Map<string, OpenChildStep>();
          let buffer = "";
          while (true) {
            const { done, value } = await reader.read();
            buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
            const split = takeSseFrames(buffer);
            buffer = split.rest;
            for (const frame of split.frames) {
              for (const event of clientEvents(frame, openSteps)) {
                if (event.type === "RUN_FINISHED" || event.type === "RUN_ERROR") sawTerminal = true;
                subscriber.next(event);
              }
            }
            if (done) break;
          }
          if (!sawTerminal || buffer.trim()) {
            throw new Error("INCOMPLETE_STREAM: connection ended before a complete terminal event");
          }
          finish();
        } catch (error) {
          if (!isAbort(error, controller)) reportClientError(error, {
            threadId: input.threadId, runId: input.runId, requestId, stage: "transport",
          });
          finish(error);
        }
      })();

      return () => {
        parent.removeEventListener("abort", onParentAbort);
        controller.abort();
        void reader?.cancel().catch(() => {});
      };
    });
  }
}

function clientEvents(frame: string, openSteps: Map<string, OpenChildStep>): BaseEvent[] {
  const data = sseData(frame);
  if (!data) return [];
  let value: unknown;
  try {
    value = JSON.parse(data);
  } catch {
    throw new Error("Invalid AG-UI event");
  }
  return takeClientEvents(value, openSteps).map((candidate) => {
    if (!candidate.parse) return candidate.event as BaseEvent;
    const parsed = EventSchemas.safeParse(candidate.event);
    if (!parsed.success) throw new Error("Invalid AG-UI event");
    return parsed.data as BaseEvent;
  });
}

function cancelledEvent(): BaseEvent {
  return { type: EventType.RUN_ERROR, message: "The run was cancelled", code: "abort" };
}

function isAbort(error: unknown, controller: AbortController): boolean {
  if (controller.signal.aborted) return true;
  if (!error || typeof error !== "object") return false;
  const name = "name" in error ? String(error.name) : "";
  const message = "message" in error ? String(error.message) : "";
  return name === "AbortError" || message === "The operation was aborted";
}
