import {
  type ConversationMessageRecord,
  type LongTermMemoryRecord,
  type LongTermMemoryRepository,
  type LongTermMemoryScope
} from "@datafoundry/metadata";
import { createHash } from "node:crypto";

const DEFAULT_MAX_CANDIDATES = 8;
const DEFAULT_MAX_MEMORY_CHARS = 900;

export type LongTermMemoryCandidate = {
  scope: LongTermMemoryScope;
  kind: string;
  content_text: string;
  confidence?: number | undefined;
};

export type LongTermMemoryExtractionInput = {
  assistantRecords: ConversationMessageRecord[];
  currentUserRecord?: ConversationMessageRecord | undefined;
  datasourceId?: string | undefined;
  runId: string;
  sessionId: string;
  signal?: AbortSignal | undefined;
  userId: string;
};

export type LongTermMemoryExtractor = {
  readonly kind: string;
  extract(input: LongTermMemoryExtractionInput): Promise<LongTermMemoryCandidate[]> | LongTermMemoryCandidate[];
};

export type LongTermMemoryServiceInput = {
  extractor?: LongTermMemoryExtractor | undefined;
  maxCandidates?: number | undefined;
  maxMemoryChars?: number | undefined;
  repository: LongTermMemoryRepository;
};

export class LongTermMemoryService {
  private readonly extractor: LongTermMemoryExtractor;
  private readonly maxCandidates: number;
  private readonly maxMemoryChars: number;
  private readonly repository: LongTermMemoryRepository;

  constructor(input: LongTermMemoryServiceInput) {
    this.extractor = input.extractor ?? new DeterministicLongTermMemoryExtractor();
    this.maxCandidates = input.maxCandidates ?? DEFAULT_MAX_CANDIDATES;
    this.maxMemoryChars = input.maxMemoryChars ?? DEFAULT_MAX_MEMORY_CHARS;
    this.repository = input.repository;
  }

  async extractAndPersist(input: LongTermMemoryExtractionInput): Promise<LongTermMemoryRecord[]> {
    const sourceMessages = [
      ...(input.currentUserRecord ? [input.currentUserRecord] : []),
      ...input.assistantRecords
    ];
    if (sourceMessages.length === 0) {
      return [];
    }
    throwIfAborted(input.signal);
    const candidates = await Promise.resolve(this.extractor.extract(input)).catch(() =>
      new DeterministicLongTermMemoryExtractor().extract(input)
    );
    throwIfAborted(input.signal);
    return candidates
      .filter((candidate: LongTermMemoryCandidate) => isPersistableCandidate(candidate, input))
      .slice(0, this.maxCandidates)
      .map((candidate: LongTermMemoryCandidate) => this.repository.upsert({
        id: createMemoryId(input, candidate),
        user_id: input.userId,
        scope: candidate.scope,
        kind: candidate.kind,
        content_text: boundText(candidate.content_text.trim(), this.maxMemoryChars),
        content: {
          extractor: this.extractor.kind,
          source_message_ids: sourceMessages.map((message) => message.id),
          text: boundText(candidate.content_text.trim(), this.maxMemoryChars)
        },
        confidence: candidate.confidence ?? 0.75,
        ...(candidate.scope === "session" ? { session_id: input.sessionId } : {}),
        ...(candidate.scope === "datasource" && input.datasourceId ? { datasource_id: input.datasourceId } : {}),
        source: this.extractor.kind,
        source_run_id: input.runId
      }));
  }
}

export class DeterministicLongTermMemoryExtractor implements LongTermMemoryExtractor {
  readonly kind = "deterministic-long-term-memory";

  extract(input: LongTermMemoryExtractionInput): LongTermMemoryCandidate[] {
    const candidates: LongTermMemoryCandidate[] = [];
    const userText = input.currentUserRecord?.content_text.trim() ?? "";
    if (isDurableUserPreference(userText)) {
      candidates.push({
        scope: "user",
        kind: "user_preference",
        content_text: userText,
        confidence: 0.7
      });
    }

    const assistantText = input.assistantRecords.map((record) => record.content_text.trim()).filter(Boolean).join("\n");
    if (assistantText && /已确认|结论|建议|后续分析|作为后续分析指标/u.test(assistantText)) {
      candidates.push({
        scope: input.datasourceId ? "datasource" : "session",
        kind: input.datasourceId ? "analysis_finding" : "session_state",
        content_text: assistantText,
        confidence: 0.65
      });
    }

    return candidates;
  }
}

export const sanitizeLongTermMemoryCandidates = (
  candidates: LongTermMemoryCandidate[],
  input: LongTermMemoryExtractionInput
): LongTermMemoryCandidate[] => candidates.filter((candidate) => isPersistableCandidate(candidate, input));

const isPersistableCandidate = (
  candidate: LongTermMemoryCandidate,
  input: LongTermMemoryExtractionInput
): boolean => {
  if (!isLongTermMemoryScope(candidate.scope) || !candidate.kind.trim() || !candidate.content_text.trim()) {
    return false;
  }
  if (candidate.scope === "datasource" && !input.datasourceId) {
    return false;
  }
  if (candidate.content_text.length > 4000) {
    return false;
  }
  return !containsSensitiveMemoryText(candidate.content_text);
};

const throwIfAborted = (signal?: AbortSignal | undefined): void => {
  if (signal?.aborted) {
    throw signal.reason instanceof Error ? signal.reason : new Error("LONG_TERM_MEMORY_ABORTED");
  }
};

const isLongTermMemoryScope = (value: unknown): value is LongTermMemoryScope =>
  value === "user" || value === "session" || value === "datasource";

const isDurableUserPreference = (text: string): boolean =>
  /记住|以后|后续|我希望|我偏好|我喜欢|不要|默认|always|prefer|remember/u.test(text);

const containsSensitiveMemoryText = (text: string): boolean =>
  /api[_-]?key|secret|password|credential|token|密钥|密码|凭证/u.test(text.toLowerCase());

const boundText = (text: string, maxChars: number): string =>
  text.length <= maxChars
    ? text
    : `${text.slice(0, maxChars)}\n[long-term memory truncated: original_chars=${text.length}]`;

const createMemoryId = (input: LongTermMemoryExtractionInput, candidate: LongTermMemoryCandidate): string =>
  `ltm:${input.sessionId}:${input.runId}:${shortHash([
    candidate.scope,
    candidate.kind,
    candidate.content_text,
    input.datasourceId ?? ""
  ].join("\n"))}`;

const shortHash = (value: string): string => createHash("sha256").update(value).digest("hex").slice(0, 16);
