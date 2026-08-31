import type { SessionOutputService } from "@datafoundry/artifacts";
import { resolve, sep } from "node:path";

import { createArtifactEvent } from "../events.js";
import type { AgentRunContext, AgUiEventEmitter } from "../types.js";

type SessionOutputIngestService = Pick<SessionOutputService, "upsertFromSessionFile">;

export type MaybeIngestSessionFileOutputInput = {
  metadata: unknown;
  sessionDir: string;
  sessionOutputService: SessionOutputIngestService;
  runContext: AgentRunContext;
  emitter: AgUiEventEmitter;
};

export type MaybeIngestSessionFileToolResultInput = {
  toolName: string;
  toolCallId?: string;
  toolInput?: unknown;
  rawResult?: unknown;
  sessionDir: string;
  sessionOutputService: SessionOutputIngestService;
  runContext: AgentRunContext;
  emitter: AgUiEventEmitter;
};

/** Ingest from Mastra `data-workspace-metadata` chunks when present. */
export const maybeIngestSessionFileOutput = async (
  input: MaybeIngestSessionFileOutputInput
): Promise<void> => {
  const metadata = isRecord(input.metadata) ? input.metadata : undefined;
  if (!metadata || !isSuccessfulWorkspaceMetadata(metadata)) {
    return;
  }
  const toolName = stringValue(metadata.toolName);
  if (toolName !== "write_file" && toolName !== "edit_file") {
    return;
  }
  const path = workspaceMetadataPath(metadata);
  if (!path) {
    return;
  }
  await ingestSessionFilePath({
    path,
    toolCallId: stringValue(metadata.toolCallId),
    sessionDir: input.sessionDir,
    sessionOutputService: input.sessionOutputService,
    runContext: input.runContext,
    emitter: input.emitter
  });
};

/**
 * Fallback ingest from governed write_file / edit_file execution.
 * Mastra does not always emit `data-workspace-metadata` for these tools; tool results
 * still carry the path (args or "Wrote N bytes to <path>" observation).
 */
export const maybeIngestSessionFileToolResult = async (
  input: MaybeIngestSessionFileToolResultInput
): Promise<void> => {
  if (input.toolName !== "write_file" && input.toolName !== "edit_file") {
    return;
  }
  if (isFailedToolResult(input.rawResult)) {
    return;
  }
  const path = pathFromToolInput(input.toolInput) ?? pathFromToolResult(input.rawResult);
  if (!path) {
    return;
  }
  await ingestSessionFilePath({
    path,
    toolCallId: input.toolCallId,
    sessionDir: input.sessionDir,
    sessionOutputService: input.sessionOutputService,
    runContext: input.runContext,
    emitter: input.emitter
  });
};

const ingestSessionFilePath = async (input: {
  path: string;
  toolCallId?: string | undefined;
  sessionDir: string;
  sessionOutputService: SessionOutputIngestService;
  runContext: AgentRunContext;
  emitter: AgUiEventEmitter;
}): Promise<void> => {
  try {
    const sourcePath = resolveSessionPath(input.sessionDir, input.path);
    const result = await input.sessionOutputService.upsertFromSessionFile({
      user_id: input.runContext.user_id,
      workspace_id: input.runContext.workspace_id ?? "default",
      session_id: input.runContext.session_id,
      run_id: input.runContext.run_id,
      path: input.path,
      source_path: sourcePath,
      ...(input.toolCallId ? { tool_call_id: input.toolCallId } : {})
    });
    if (result) {
      input.emitter.emit(createArtifactEvent(result.artifact));
    }
  } catch (error) {
    console.warn("[data-foundry] session_output_ingest_failed", error);
  }
};

const isSuccessfulWorkspaceMetadata = (metadata: Record<string, unknown>): boolean => {
  const status = stringValue(metadata.status)?.toLowerCase();
  return !status || status === "ok" || status === "ready" || status === "success" || status === "succeeded";
};

const workspaceMetadataPath = (metadata: Record<string, unknown>): string | undefined =>
  stringValue(metadata.path)
  ?? stringValue(metadata.filePath)
  ?? stringValue(metadata.filename)
  ?? (isRecord(metadata.args) ? stringValue(metadata.args.path) : undefined)
  ?? (isRecord(metadata.input) ? stringValue(metadata.input.path) : undefined);

const pathFromToolInput = (toolInput: unknown): string | undefined => {
  if (!isRecord(toolInput)) {
    return undefined;
  }
  return stringValue(toolInput.path) ?? stringValue(toolInput.filePath) ?? stringValue(toolInput.filename);
};

const pathFromToolResult = (rawResult: unknown): string | undefined => {
  if (typeof rawResult === "string") {
    return pathFromObservationText(rawResult);
  }
  if (!isRecord(rawResult)) {
    return undefined;
  }
  return pathFromToolInput(rawResult)
    ?? (typeof rawResult.observation === "string" ? pathFromObservationText(rawResult.observation) : undefined)
    ?? (typeof rawResult.message === "string" ? pathFromObservationText(rawResult.message) : undefined);
};

/** Parse Mastra workspace observations like `Wrote 3481 bytes to order_analysis_report.md`. */
const pathFromObservationText = (text: string): string | undefined => {
  const wrote = text.match(/\b(?:Wrote|Updated|Edited)\b(?:\s+\d+\s+bytes)?\s+to\s+(.+?)\s*$/iu);
  if (wrote?.[1]) {
    return wrote[1].trim().replace(/^[`"']+|[`"']+$/gu, "");
  }
  return undefined;
};

const isFailedToolResult = (rawResult: unknown): boolean => {
  if (!isRecord(rawResult)) {
    return false;
  }
  if (rawResult.isError === true) {
    return true;
  }
  const status = stringValue(rawResult.status)?.toLowerCase();
  return status === "failed" || status === "error";
};

const resolveSessionPath = (sessionDir: string, path: string): string => {
  const root = resolve(sessionDir);
  const resolved = resolve(root, path);
  if (resolved !== root && !resolved.startsWith(`${root}${sep}`)) {
    throw new Error("SESSION_OUTPUT_PATH_ESCAPE");
  }
  return resolved;
};

const stringValue = (value: unknown): string | undefined =>
  typeof value === "string" && value.length > 0 ? value : undefined;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
