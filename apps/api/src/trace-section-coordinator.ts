import { EventType } from "@ag-ui/client";
import type { RunEventEnvelope } from "@datafoundry/contracts";
import type { MetadataStore, TraceSectionRecord } from "@datafoundry/metadata";
import { Agent } from "@mastra/core/agent";

const CONTEXT_STEPS_PER_SECTION_CHECK = 3;
const MAX_CONTEXT_STEPS_PER_SUMMARY_CHUNK = 3;
const MAX_CONTEXT_STEPS_PER_SECTION = 16;
const MAX_EVENT_PREVIEW_LENGTH = 1_200;
const MAX_SECTION_INPUT_LENGTH = 12_000;
const OMITTED_EVENT_LOG_MARKER = "\n... middle trace events omitted ...\n";
const SUMMARY_GENERATION_ATTEMPTS = 3;
const SUMMARY_RETRY_DELAY_MS = 750;

type TraceSummaryModel = {
  kind: "openai-compatible";
  model: unknown;
  model_name: string;
};

type PendingSummary = {
  endEventSeq: number;
  forceCompletion: boolean;
};

type TraceSectionSummary = {
  phaseKey: string;
  summary: string;
  title: string;
};

/** Summarize durable trace ranges without delaying the agent event stream. */
export class TraceSectionCoordinator {
  private readonly agent: Agent;
  private readonly pendingByRun = new Map<string, PendingSummary>();
  private readonly processingRuns = new Set<string>();

  constructor(
    private readonly metadataStore: MetadataStore,
    private readonly modelProvider: TraceSummaryModel,
    private readonly userId: string
  ) {
    this.agent = new Agent({
      id: "trace-section-summarizer",
      name: "Trace Section Summarizer",
      instructions: "You turn agent execution evidence into concise, user-facing task phases.",
      model: modelProvider.model as never,
      defaultOptions: {
        maxSteps: 1,
        providerOptions: {
          openai: { systemMessageMode: "system" }
        }
      }
    });
  }

  observe(envelope: RunEventEnvelope): void {
    const event = recordValue(envelope.event);
    if (!event) {
      return;
    }
    if (isContextCompiled(event)) {
      const contextStepCount = this.metadataStore.checkpoints
        .listByRun({ user_id: this.userId, run_id: envelope.run_id })
        .filter((checkpoint) => checkpoint.kind === "context-compiled").length;
      if (contextStepCount >= CONTEXT_STEPS_PER_SECTION_CHECK
        && contextStepCount % CONTEXT_STEPS_PER_SECTION_CHECK === 0) {
        this.schedule(envelope, false);
      }
      return;
    }
    if (event.type === EventType.RUN_FINISHED || event.type === EventType.RUN_ERROR) {
      this.schedule(envelope, true);
    }
  }

  private schedule(envelope: RunEventEnvelope, forceCompletion: boolean): void {
    const pending = this.pendingByRun.get(envelope.run_id);
    this.pendingByRun.set(envelope.run_id, {
      endEventSeq: Math.max(pending?.endEventSeq ?? 0, envelope.seq),
      forceCompletion: pending?.forceCompletion || forceCompletion
    });
    if (this.processingRuns.has(envelope.run_id)) {
      return;
    }
    queueMicrotask(() => {
      void this.processRun(envelope.run_id, envelope.session_id);
    });
  }

  private async processRun(runId: string, sessionId: string): Promise<void> {
    if (this.processingRuns.has(runId)) {
      return;
    }
    this.processingRuns.add(runId);
    try {
      while (true) {
        const pending = this.pendingByRun.get(runId);
        if (!pending) {
          return;
        }
        this.pendingByRun.delete(runId);
        const consumedEndEventSeq = await this.summarizeRange({ ...pending, runId, sessionId });
        if (consumedEndEventSeq < pending.endEventSeq) {
          const queued = this.pendingByRun.get(runId);
          this.pendingByRun.set(runId, {
            endEventSeq: Math.max(pending.endEventSeq, queued?.endEventSeq ?? 0),
            forceCompletion: pending.forceCompletion || queued?.forceCompletion === true
          });
        }
      }
    } finally {
      this.processingRuns.delete(runId);
      if (this.pendingByRun.has(runId)) {
        queueMicrotask(() => {
          void this.processRun(runId, sessionId);
        });
      }
    }
  }

  private async summarizeRange(input: PendingSummary & { runId: string; sessionId: string }): Promise<number> {
    const existingSections = this.metadataStore.traceSections.listByRun({
      user_id: this.userId,
      run_id: input.runId
    });
    const latestSection = existingSections.at(-1);
    const startEventSeq = (latestSection?.end_event_seq ?? 0) + 1;
    if (input.endEventSeq < startEventSeq) {
      return input.endEventSeq;
    }
    const runCheckpoints = this.metadataStore.checkpoints.listByRun({
      user_id: this.userId,
      run_id: input.runId
    });
    const contextCheckpoints = runCheckpoints
      .filter((checkpoint) => checkpoint.kind === "context-compiled"
        && checkpoint.event_seq >= startEventSeq
        && checkpoint.event_seq <= input.endEventSeq);
    const nextChunkCheckpoint = contextCheckpoints.at(MAX_CONTEXT_STEPS_PER_SUMMARY_CHUNK);
    if (!nextChunkCheckpoint && !input.forceCompletion) {
      return input.endEventSeq;
    }
    const endEventSeq = nextChunkCheckpoint ? nextChunkCheckpoint.event_seq - 1 : input.endEventSeq;
    if (contextCheckpoints.length === 0 && input.forceCompletion && latestSection) {
      this.metadataStore.traceSections.upsert({
        id: latestSection.id,
        user_id: this.userId,
        session_id: input.sessionId,
        run_id: input.runId,
        branch_id: latestSection.branch_id,
        phase_key: latestSection.phase_key,
        start_event_seq: latestSection.start_event_seq,
        end_event_seq: input.endEventSeq,
        status: "completed",
        title: latestSection.title,
        summary: latestSection.summary
      });
      return input.endEventSeq;
    }
    const events = this.metadataStore.runEvents.listByRun({ user_id: this.userId, run_id: input.runId })
      .filter((event) => event.seq >= startEventSeq && event.seq <= endEventSeq);
    if (events.length === 0) {
      return endEventSeq;
    }

    const taskObjective = this.metadataStore.runs.find({ user_id: this.userId, run_id: input.runId })?.user_input;
    const fallback = fallbackSummary(startEventSeq, endEventSeq, events, latestSection, taskObjective);
    let summary = fallback;
    try {
      const resultText = await this.generateSummary(buildSummaryPrompt({
        endEventSeq,
        events,
        modelName: this.modelProvider.model_name,
        previousSections: existingSections,
        startEventSeq,
        ...(taskObjective ? { taskObjective } : {})
      }));
      summary = parseSummary(resultText, fallback);
    } catch {
      summary = fallback;
    }

    const candidateContextCount = contextCheckpoints
      .filter((checkpoint) => checkpoint.event_seq <= endEventSeq)
      .length;
    const latestSectionContextCount = latestSection
      ? runCheckpoints.filter((checkpoint) => checkpoint.kind === "context-compiled"
        && checkpoint.event_seq >= latestSection.start_event_seq
        && checkpoint.event_seq <= latestSection.end_event_seq).length
      : 0;
    const mergeTarget = latestSection?.phase_key === summary.phaseKey
      && latestSectionContextCount + candidateContextCount <= MAX_CONTEXT_STEPS_PER_SECTION
      ? latestSection
      : undefined;
    if (latestSection?.status === "in-progress" && !mergeTarget) {
      this.metadataStore.traceSections.upsert({
        id: latestSection.id,
        user_id: this.userId,
        session_id: input.sessionId,
        run_id: input.runId,
        branch_id: latestSection.branch_id,
        phase_key: latestSection.phase_key,
        start_event_seq: latestSection.start_event_seq,
        end_event_seq: latestSection.end_event_seq,
        status: "completed",
        title: latestSection.title,
        summary: latestSection.summary
      });
    }
    const reachedTerminalRange = input.forceCompletion && endEventSeq === input.endEventSeq;
    this.metadataStore.traceSections.upsert({
      id: mergeTarget?.id ?? `trace-section:${input.runId}:${startEventSeq}`,
      user_id: this.userId,
      session_id: input.sessionId,
      run_id: input.runId,
      ...(mergeTarget ? { branch_id: mergeTarget.branch_id } : {}),
      phase_key: summary.phaseKey,
      start_event_seq: mergeTarget?.start_event_seq ?? startEventSeq,
      end_event_seq: endEventSeq,
      status: reachedTerminalRange ? "completed" : "in-progress",
      title: summary.title,
      summary: summary.summary
    });
    return endEventSeq;
  }

  private async generateSummary(prompt: string): Promise<string> {
    let lastError: unknown;
    for (let attempt = 1; attempt <= SUMMARY_GENERATION_ATTEMPTS; attempt += 1) {
      try {
        const result = await this.agent.generate(prompt);
        return result.text;
      } catch (error) {
        lastError = error;
        if (attempt < SUMMARY_GENERATION_ATTEMPTS) {
          await delay(SUMMARY_RETRY_DELAY_MS * attempt);
        }
      }
    }
    throw lastError;
  }
}

function buildSummaryPrompt(input: {
  endEventSeq: number;
  events: Array<{ event_type: string; payload_json: string; seq: number }>;
  modelName: string;
  previousSections: TraceSectionRecord[];
  startEventSeq: number;
  taskObjective?: string;
}): string {
  const eventLog = truncateMiddle(input.events.map(summarizeEvent).join("\n"), MAX_SECTION_INPUT_LENGTH);
  const completedSections = input.previousSections.filter((section) => section.status === "completed");
  const activeSection = input.previousSections.filter((section) => section.status === "in-progress").at(-1);
  const phaseIndex = completedSections.length > 0
    ? completedSections
      .map((section) => [
        `- ${section.phase_key} | ${section.title}`,
        `events ${section.start_event_seq}-${section.end_event_seq}`
      ].join(" | "))
      .join("\n")
    : "- None yet";
  const recentPhaseDetails = input.previousSections.length > 0
    ? input.previousSections.slice(-4)
      .map((section) => `- ${section.phase_key} | ${section.title}: ${section.summary}`)
      .join("\n")
    : "- None yet";
  return [
    "You are the task historian for a long-running data agent.",
    `The task selected model is ${input.modelName}.`,
    `Task objective: ${input.taskObjective ?? "Not available"}`,
    "Completed phase index:",
    phaseIndex,
    "Current active phase:",
    activeSection
      ? `${activeSection.phase_key} | ${activeSection.title}: ${activeSection.summary}`
      : "- None yet",
    "Recent phase details:",
    recentPhaseDetails,
    "Read the candidate evidence as a bounded slice of the task's progress. Describe the concrete question being",
    "investigated or the deliverable being produced, rather than listing tool mechanics. Use the evidence to decide",
    "whether this piece of work has naturally reached a result or handoff to another part of the task.",
    "Choose an inspectable concrete subtask as the phase identity rather than a broad workstream covering several",
    "different questions or deliverables.",
    "Give the phase a stable kebab-case phaseKey. Reuse a phaseKey from the index when this evidence continues the",
    "same concrete question or deliverable. A shared datasource, tool type, or overall objective is not enough to make",
    "two pieces of work the same phase. When reusing a key, write a concise cumulative summary that remains",
    "accurate for the combined phase. Use a new phaseKey when the task has moved to meaningfully different work.",
    "Write the title in the same language as the task objective. It should read naturally in a progress update.",
    "Return exactly one JSON object and no markdown:",
    '{"phaseKey":"stable-phase-key","title":"short subtask title","summary":"one factual sentence"}',
    "Do not mention credentials or repeat sensitive values. Do not invent actions.",
    `Event range: ${input.startEventSeq}-${input.endEventSeq}`,
    "Trace events:",
    eventLog
  ].join("\n");
}

function summarizeEvent(event: { event_type: string; payload_json: string; seq: number }): string {
  const payload = parseRecord(event.payload_json);
  const parts = [
    `#${event.seq}`,
    event.event_type,
    stringValue(payload.name),
    stringValue(payload.toolCallName) ?? stringValue(payload.toolName) ?? stringValue(payload.tool_name),
    previewPayload(payload)
  ].filter((part): part is string => Boolean(part));
  return parts.join(" | ");
}

function previewPayload(payload: Record<string, unknown>): string | undefined {
  const preview = {
    args: payload.delta ?? payload.argsText ?? payload.input,
    content: payload.content,
    message: payload.message,
    value: payload.value
  };
  const text = redactSecrets(JSON.stringify(preview));
  return text ? truncate(text, MAX_EVENT_PREVIEW_LENGTH) : undefined;
}

function parseSummary(text: string, fallback: TraceSectionSummary): TraceSectionSummary {
  const normalized = text.trim().replace(/^```(?:json)?\s*/iu, "").replace(/\s*```$/u, "");
  const start = normalized.indexOf("{");
  const end = normalized.lastIndexOf("}");
  try {
    const parsed = recordValue(JSON.parse(start >= 0 && end >= start ? normalized.slice(start, end + 1) : normalized));
    const phaseKey = normalizePhaseKey(stringValue(parsed?.phaseKey));
    const title = stringValue(parsed?.title);
    const summary = stringValue(parsed?.summary);
    if (!phaseKey || !title || !summary) {
      return fallback;
    }
    return {
      phaseKey,
      summary: truncate(summary, 600),
      title: truncate(title, 120)
    };
  } catch {
    return fallback;
  }
}

function fallbackSummary(
  startEventSeq: number,
  endEventSeq: number,
  events: Array<{ event_type: string; payload_json: string; seq: number }>,
  latestSection?: TraceSectionRecord,
  taskObjective?: string
): TraceSectionSummary {
  const toolNames = events.flatMap((event) => {
    const payload = parseRecord(event.payload_json);
    const name = stringValue(payload.toolCallName) ?? stringValue(payload.toolName) ?? stringValue(payload.tool_name);
    return name ? [name] : [];
  });
  const title = latestSection?.title ?? `Trace steps ${startEventSeq}-${endEventSeq}`;
  const detail = toolNames.length > 0
    ? `Used ${[...new Set(toolNames)].join(", ")}.`
    : "Recorded agent reasoning and output.";
  const outputTools = new Set(["edit_file", "promote_workspace_file", "write_file"]);
  if (toolNames.some((name) => outputTools.has(name))) {
    const usesChinese = taskObjective ? /\p{Script=Han}/u.test(taskObjective) : false;
    return {
      phaseKey: "report-generation",
      summary: usesChinese ? "生成任务报告和可下载产物。" : "Generated the task report and downloadable artifacts.",
      title: usesChinese ? "生成报告与可下载产物" : "Generate report and downloadable artifacts"
    };
  }
  return {
    phaseKey: latestSection?.phase_key ?? `trace-${startEventSeq}`,
    summary: latestSection?.summary ?? detail,
    title
  };
}

function isContextCompiled(event: Record<string, unknown>): boolean {
  return event.type === EventType.CUSTOM && event.name === "context.compiled";
}

function parseRecord(value: string): Record<string, unknown> {
  try {
    return recordValue(JSON.parse(value)) ?? {};
  } catch {
    return {};
  }
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : undefined;
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function normalizePhaseKey(value: string | undefined): string | undefined {
  if (!value) {
    return undefined;
  }
  const normalized = value.toLowerCase()
    .replace(/[^\p{L}\p{N}._:-]+/gu, "-")
    .replace(/^-+|-+$/gu, "");
  return normalized ? truncate(normalized, 80) : undefined;
}

function redactSecrets(value: string): string {
  return value.replace(/(api[_-]?key|authorization|password|secret|token)\s*[=:]\s*[^,\s}"']+/giu, "$1=[redacted]");
}

function truncate(value: string, maxLength: number): string {
  return value.length > maxLength ? `${value.slice(0, maxLength)}...` : value;
}

function truncateMiddle(value: string, maxLength: number): string {
  if (value.length <= maxLength) {
    return value;
  }
  const availableLength = maxLength - OMITTED_EVENT_LOG_MARKER.length;
  const startLength = Math.floor(availableLength * 0.4);
  const endLength = availableLength - startLength;
  return `${value.slice(0, startLength)}${OMITTED_EVENT_LOG_MARKER}${value.slice(-endLength)}`;
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
