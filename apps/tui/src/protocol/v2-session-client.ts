import { MessageSchema, type Message } from "@ag-ui/core";
import { z } from "zod";
import type { SessionListItem } from "../config/index.js";
import type { DisplayMessage } from "../state/tui-state.js";
import type { LiveToolCallRecord } from "../state/live-run-state.js";
import type { DataArtifact } from "../state/data-task-state.js";
import { AguiClientError } from "./agui-client.js";

const summary = z.object({
  threadId: z.string(), title: z.string(), createdAt: z.string(), updatedAt: z.string(),
  status: z.enum(["running", "complete", "error", "interrupted"]), hasCheckpoint: z.boolean(),
});
const session = summary.extend({
  messages: z.array(z.unknown().transform((value) => MessageSchema.parse(value))),
  newThreadRequired: z.boolean(), notice: z.string().nullable(),
});
export type V2Session = z.infer<typeof session>;

export class V2SessionClient {
  constructor(private baseUrl: string, private fetchImpl: typeof fetch) {}

  private async read(path: string): Promise<unknown> {
    const response = await this.fetchImpl(this.baseUrl + path, { signal: AbortSignal.timeout(10_000) });
    const body = await response.json() as { detail?: unknown };
    if (!response.ok) throw new AguiClientError(
      typeof body.detail === "string" ? body.detail : `Session request failed (${response.status})`,
      "HTTP_ERROR", response.status,
    );
    return body;
  }

  async listSessions(options = { limit: 50 }): Promise<{ sessions: SessionListItem[] }> {
    const value = z.object({ sessions: z.array(summary) }).parse(await this.read(`/sessions?limit=${options.limit}`));
    return { sessions: value.sessions.map((entry) => ({ ...entry, id: entry.threadId })) };
  }

  async getSession(id: string): Promise<V2Session> {
    return session.parse(await this.read(`/sessions/${encodeURIComponent(id)}`));
  }

  async listOutputs(threadId: string): Promise<DataArtifact[]> {
    const value = z.object({ files: z.array(z.object({
      path: z.string(), size: z.number().nonnegative(), modifiedAt: z.string(),
    })) }).parse(await this.read(`/sessions/${encodeURIComponent(threadId)}/outputs`));
    return value.files.map((file) => ({
      id: file.path, title: file.path, kind: "file", type: "file",
      summary: `Session outputs: ${file.path}`, previewAvailable: true,
      recordedAtMs: Date.parse(file.modifiedAt),
      detail: { type: "file", path: file.path, size: file.size, mtime: file.modifiedAt },
    }));
  }

  async getOutputPreview(threadId: string, path: string): Promise<unknown> {
    return this.read(`/sessions/${encodeURIComponent(threadId)}/outputs/preview?path=${encodeURIComponent(path)}`);
  }
}

/** Translate native AG-UI history directly into the existing Ink presentation model. */
export function restoreV2Messages(input: Message[]) {
  const messages: DisplayMessage[] = [];
  const tools = new Map<string, LiveToolCallRecord>();
  const timestamp = Date.now();
  let assistant: DisplayMessage | undefined;
  for (const message of input) {
    if (message.role === "user") {
      messages.push({ id: message.id, role: "user", timestamp, elements: [{
        type: "text", content: typeof message.content === "string" ? message.content : "[Non-text message]", timestamp,
      }] });
      assistant = undefined;
    } else if (message.role === "assistant") {
      if (!assistant) {
        assistant = { id: message.id, role: "assistant", timestamp, elements: [] };
        messages.push(assistant);
      }
      if (typeof message.content === "string" && message.content) {
        assistant.elements.push({ type: "text", content: message.content, timestamp });
      }
      for (const call of message.toolCalls ?? []) {
        const record: LiveToolCallRecord = {
          id: call.id, name: call.function.name, status: "pending", args: call.function.arguments,
        };
        tools.set(call.id, record);
        assistant.elements.push({ type: "tool_call", toolCallId: call.id, timestamp, toolCall: record });
      }
    } else if (message.role === "tool") {
      const record = tools.get(message.toolCallId);
      if (record) {
        record.status = message.error ? "failed" : "success";
        record.result = message.content;
      }
    }
  }
  return { messages, toolCalls: [...tools.values()], artifacts: [] };
}

export interface V2AppContext {
  sessions: V2SessionClient;
  model: string;
  home: string;
  mounts?: string | undefined;
}
