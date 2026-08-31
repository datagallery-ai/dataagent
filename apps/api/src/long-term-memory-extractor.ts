import { Agent } from "@mastra/core/agent";

import {
  DeterministicLongTermMemoryExtractor,
  type LongTermMemoryCandidate,
  type LongTermMemoryExtractionInput,
  type LongTermMemoryExtractor,
  sanitizeLongTermMemoryCandidates
} from "./long-term-memory.js";

export type MastraLongTermMemoryExtractorInput = {
  maxOutputTokens?: number | undefined;
  model: unknown;
  temperature?: number | undefined;
};

export class MastraLongTermMemoryExtractor implements LongTermMemoryExtractor {
  readonly kind = "mastra-long-term-memory";
  private readonly agent: Agent;
  private readonly fallback = new DeterministicLongTermMemoryExtractor();
  private readonly maxOutputTokens: number;
  private readonly temperature: number;

  constructor(input: MastraLongTermMemoryExtractorInput) {
    this.maxOutputTokens = input.maxOutputTokens ?? 900;
    this.temperature = input.temperature ?? 0;
    this.agent = new Agent({
      id: "long-term-memory-extractor",
      name: "Long-Term Memory Extractor",
      instructions: [
        "You extract durable long-term memory for a data analysis agent.",
        "Return only strict JSON. No markdown, prose, or XML.",
        "Extract only stable user preferences, user constraints, durable project facts, verified dataset facts,",
        "analysis decisions, or open analysis state likely useful in future runs.",
        "Do not extract transient filler, raw SQL text, credentials, secrets, private environment values,",
        "unverified schema claims, or tool output not present in the source messages.",
        "Prefer Chinese content_text if source messages are Chinese."
      ].join("\n"),
      model: input.model as never,
      defaultOptions: {
        maxSteps: 1,
        modelSettings: {
          maxOutputTokens: this.maxOutputTokens,
          temperature: this.temperature
        },
        providerOptions: {
          openai: {
            systemMessageMode: "system"
          }
        }
      }
    });
  }

  async extract(input: LongTermMemoryExtractionInput): Promise<LongTermMemoryCandidate[]> {
    try {
      const output = await this.agent.generate(buildExtractorPrompt(input));
      const parsed = parseCandidateJson(output.text);
      const candidates = sanitizeLongTermMemoryCandidates(parsed, input).slice(0, 8);
      return candidates.length > 0 ? candidates : this.fallback.extract(input);
    } catch {
      return this.fallback.extract(input);
    }
  }
}

export const createMastraLongTermMemoryExtractor = (
  input: MastraLongTermMemoryExtractorInput
): MastraLongTermMemoryExtractor => new MastraLongTermMemoryExtractor(input);

const buildExtractorPrompt = (input: LongTermMemoryExtractionInput): string => {
  const messages = [
    ...(input.currentUserRecord ? [input.currentUserRecord] : []),
    ...input.assistantRecords
  ].map((message) => [
    `<message role="${message.role}" id="${message.id}">`,
    message.content_text,
    "</message>"
  ].join("\n")).join("\n");
  return [
    "Extract up to 6 long-term memory candidates from these completed run messages.",
    "Return JSON array only. Each item must have:",
    [
      "{",
      "  \"scope\": \"user\" | \"session\" | \"datasource\",",
      "  \"kind\": \"user_preference\" | \"user_constraint\" | \"dataset_fact\" |",
      "    \"analysis_finding\" | \"decision\" | \"session_state\",",
      "  \"content_text\": \"concise durable fact\",",
      "  \"confidence\": 0.0-1.0",
      "}"
    ].join("\n"),
    "Use datasource scope only for facts tied to the active datasource.",
    "Use session scope for open analysis state useful within this conversation.",
    "Use user scope only for stable user preferences or constraints.",
    input.datasourceId ? `Active datasource: ${input.datasourceId}` : "",
    "<source_messages>",
    messages,
    "</source_messages>"
  ].filter(Boolean).join("\n\n");
};

const parseCandidateJson = (text: string): LongTermMemoryCandidate[] => {
  const jsonText = extractJsonArray(text);
  const parsed = JSON.parse(jsonText) as unknown;
  return Array.isArray(parsed) ? parsed.filter(isCandidate) : [];
};

const extractJsonArray = (text: string): string => {
  const trimmed = text.trim().replace(/^```(?:json)?/u, "").replace(/```$/u, "").trim();
  const start = trimmed.indexOf("[");
  const end = trimmed.lastIndexOf("]");
  if (start === -1 || end === -1 || end < start) {
    throw new Error("LONG_TERM_MEMORY_EXTRACTOR_JSON_ARRAY_NOT_FOUND");
  }
  return trimmed.slice(start, end + 1);
};

const isCandidate = (value: unknown): value is LongTermMemoryCandidate => {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    (record.scope === "user" || record.scope === "session" || record.scope === "datasource") &&
    typeof record.kind === "string" &&
    typeof record.content_text === "string" &&
    (record.confidence === undefined || typeof record.confidence === "number")
  );
};
