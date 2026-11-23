import type { SessionOutputFileDto, SessionOutputPreviewDto } from "../../lib/config-api/types";
import type { DataArtifact } from "./data-task-state";

/**
 * Files the agent writes to its session output directory are listed by the conversation
 * server (`GET /sessions/{id}/outputs`), not by the `/api/v1/artifacts` product API.
 * These helpers turn that listing into AG-UI `artifact` event values so the Outputs tab
 * renders them like any other artifact.
 */

export const SESSION_OUTPUT_KEY_PREFIX = "session_file:";

/** Matches the backend preview cap (serve/outputs.py PREVIEW_BYTES). */
export const SESSION_OUTPUT_PREVIEW_MAX_BYTES = 64 * 1024;

/** Newest files whose text is inlined up front; older ones still list with their path. */
export const SESSION_OUTPUT_PREVIEW_LIMIT = 12;

const TEXT_OUTPUT_PATTERN =
  /\.(?:md|markdown|mdx|txt|csv|tsv|json|jsonl|sql|py|js|ts|html?|ya?ml|xml|log)$/iu;

export function sessionOutputKey(path: string): string {
  return `${SESSION_OUTPUT_KEY_PREFIX}${path}`;
}

export function isSessionOutputKey(value: string | undefined): boolean {
  return Boolean(value?.startsWith(SESSION_OUTPUT_KEY_PREFIX));
}

export function shouldPreviewSessionOutput(file: SessionOutputFileDto): boolean {
  return file.size <= SESSION_OUTPUT_PREVIEW_MAX_BYTES && TEXT_OUTPUT_PATTERN.test(file.path);
}

/** Paths whose preview should be fetched: newest first, text only, capped. */
export function sessionOutputPreviewPaths(files: SessionOutputFileDto[]): string[] {
  return files
    .filter(shouldPreviewSessionOutput)
    .slice(0, SESSION_OUTPUT_PREVIEW_LIMIT)
    .map((file) => file.path);
}

export function sessionOutputArtifactValue(
  file: SessionOutputFileDto,
  preview?: Pick<SessionOutputPreviewDto, "content">,
): Record<string, unknown> {
  const name = file.path.split("/").filter(Boolean).pop() ?? file.path;
  return {
    id: sessionOutputKey(file.path),
    type: "file",
    name,
    title: name,
    logical_key: sessionOutputKey(file.path),
    created_at: file.modifiedAt,
    preview_json: {
      path: file.path,
      size: file.size,
      mtime: file.modifiedAt,
      ...(preview?.content !== undefined ? { content: preview.content } : {}),
    },
  };
}

function mimeTypeForOutput(path: string): string {
  if (/\.(?:md|markdown|mdx)$/iu.test(path)) return "text/markdown;charset=utf-8";
  if (/\.csv$/iu.test(path)) return "text/csv;charset=utf-8";
  if (/\.json$/iu.test(path)) return "application/json;charset=utf-8";
  if (/\.html?$/iu.test(path)) return "text/html;charset=utf-8";
  return "text/plain;charset=utf-8";
}

type SessionOutputArtifact = Pick<DataArtifact, "logicalKey" | "detail" | "title">;

/**
 * Session outputs have no download endpoint; text files under the preview cap carry their
 * full content inline, so they download from memory. Anything else is not downloadable here.
 */
export function sessionOutputDownload(
  artifact: SessionOutputArtifact,
): { blob: Blob; filename: string } | null {
  if (!isSessionOutputKey(artifact.logicalKey)) return null;
  const detail = artifact.detail;
  if (detail?.type !== "file" || typeof detail.content !== "string") return null;
  return {
    blob: new Blob([detail.content], { type: mimeTypeForOutput(detail.path) }),
    filename: artifact.title,
  };
}

/** Regular artifacts defer to the export API; session outputs need inline content. */
export function canDownloadArtifact(artifact: SessionOutputArtifact): boolean {
  return !isSessionOutputKey(artifact.logicalKey) || sessionOutputDownload(artifact) !== null;
}
