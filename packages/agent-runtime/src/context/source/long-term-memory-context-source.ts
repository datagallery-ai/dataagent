import { createHash } from "node:crypto";

import { createContextItem, type ContextItem } from "../inventory/context-item.js";
import { createContextSourceMetadata } from "../inventory/context-source-metadata.js";
import type { RuntimeContextSource, RuntimeContextSourceInput } from "./runtime-context-source.js";

const DEFAULT_MAX_MEMORY_CHARS = 6000;

export type LongTermMemoryScope = "user" | "session" | "datasource";

export type LongTermMemoryContextRecord = {
  id: string;
  scope: LongTermMemoryScope;
  kind: string;
  content_text: string;
  confidence: number;
  session_id?: string;
  datasource_id?: string;
  source?: string;
  source_run_id?: string;
};

export type LongTermMemoryContextSourceOptions = {
  records: LongTermMemoryContextRecord[];
  maxChars?: number;
};

export class LongTermMemoryContextSource implements RuntimeContextSource {
  readonly sourceType = "long-term-memory";

  constructor(private readonly options: LongTermMemoryContextSourceOptions) {}

  collect(input: RuntimeContextSourceInput): ContextItem[] {
    const memories = normalizeMemories(this.options.records);
    if (memories.length === 0) {
      return [];
    }
    const content = createLongTermMemoryContextText(
      memories,
      this.options.maxChars ?? input.budget.maxChars ?? DEFAULT_MAX_MEMORY_CHARS
    );
    return [
      createContextItem({
        id: "long-term-memory-model",
        sourceType: this.sourceType,
        sourceId: "long-term-memory",
        groupId: "long-term-memory",
        visibility: "model",
        trust: "memory",
        retention: "supporting",
        priority: 35,
        content,
        metadata: createContextSourceMetadata({
          dedupeKeys: memories.map((memory) => `long-term-memory:${memory.id}`),
          exclusivityKey: "long-term-memory:metadata-ltm",
          overlapKeys: memories.flatMap((memory) => [
            `ltm:${memory.id}`,
            contentOverlapKey(memory.content_text)
          ]),
          scope: createLongTermMemoryScope(memories),
          sourceKind: "long-term-memory",
          sourceOwner: "metadata-ltm"
        }, {
          atomic: false,
          groupKind: "source",
          memoryIds: memories.map((memory) => memory.id)
        })
      })
    ];
  }
}

export const createLongTermMemoryContextText = (
  memories: LongTermMemoryContextRecord[],
  maxChars = DEFAULT_MAX_MEMORY_CHARS
): string => {
  const lines = [
    "<long_term_memory>",
    ...memories.map((memory) => {
      const scope = memory.scope === "datasource" && memory.datasource_id
        ? `datasource:${memory.datasource_id}`
        : memory.scope === "session" && memory.session_id
          ? `session:${memory.session_id}`
          : memory.scope;
      return [
        `- id=${escapeAttribute(memory.id)}`,
        `kind=${escapeAttribute(memory.kind)}`,
        `scope=${escapeAttribute(scope)}`,
        `confidence=${memory.confidence.toFixed(2)}:`,
        escapeText(memory.content_text)
      ].join(" ");
    }),
    "</long_term_memory>"
  ];
  return boundText(lines.join("\n"), maxChars);
};

const normalizeMemories = (raw: unknown): LongTermMemoryContextRecord[] =>
  Array.isArray(raw) ? raw.filter(isLongTermMemoryContextRecord) : [];

const isLongTermMemoryContextRecord = (value: unknown): value is LongTermMemoryContextRecord => {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    typeof record.id === "string" &&
    isLongTermMemoryScope(record.scope) &&
    typeof record.kind === "string" &&
    typeof record.content_text === "string" &&
    typeof record.confidence === "number"
  );
};

const isLongTermMemoryScope = (value: unknown): value is LongTermMemoryScope =>
  value === "user" || value === "session" || value === "datasource";

const createLongTermMemoryScope = (memories: LongTermMemoryContextRecord[]) => {
  const sessionId = memories.find((memory) => memory.session_id)?.session_id;
  const datasourceId = memories.find((memory) => memory.datasource_id)?.datasource_id;
  return {
    ...(datasourceId ? { datasourceId } : {}),
    ...(sessionId ? { sessionId } : {})
  };
};

const boundText = (text: string, maxChars: number): string => {
  if (text.length <= maxChars) {
    return text;
  }
  return `${text.slice(0, Math.max(maxChars - 80, 0))}\n[long-term memory truncated: original_chars=${text.length}]`;
};

const escapeAttribute = (value: string): string => value.replaceAll('"', "&quot;").replaceAll("<", "&lt;");

const escapeText = (value: string): string => value.replaceAll("<", "&lt;").replaceAll(">", "&gt;");

const contentOverlapKey = (text: string): string =>
  `content:${createHash("sha256").update(normalizeOverlapText(text)).digest("hex")}`;

const normalizeOverlapText = (text: string): string => text.toLowerCase().replaceAll(/\s+/gu, " ").trim();
