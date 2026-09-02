import type { BaseEvent } from "@ag-ui/client";

/** Encode one AG-UI event as an SSE `data:` frame. */
export const encodeAgUiSseEvent = (event: BaseEvent): string =>
  `data: ${JSON.stringify(event)}\n\n`;

export type SseParseResult = {
  events: unknown[];
  remainder: string;
};

/** Incremental SSE parser for AG-UI event streams. */
export const consumeSseBuffer = (buffer: string): SseParseResult => {
  const events: unknown[] = [];
  let remainder = buffer.replace(/\r\n/g, "\n");
  while (true) {
    const boundary = remainder.indexOf("\n\n");
    if (boundary < 0) {
      break;
    }
    const frame = remainder.slice(0, boundary);
    remainder = remainder.slice(boundary + 2);
    const dataLines = frame
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart());
    if (dataLines.length === 0) {
      continue;
    }
    const payload = dataLines.join("\n");
    if (!payload || payload === "[DONE]") {
      continue;
    }
    events.push(JSON.parse(payload) as unknown);
  }
  return { events, remainder };
};

export const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;
