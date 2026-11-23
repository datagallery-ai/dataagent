import type { FileAssetRefDto } from "../../lib/config-api";

export type WorkspaceFileUploadApi = {
  uploadWorkspaceFiles: (
    files: File[],
    sessionId?: string | null,
  ) => Promise<{ files: FileAssetRefDto[] }>;
  promoteWorkspaceFile: (id: string) => Promise<FileAssetRefDto>;
};

export type WorkspaceUploadPromoteFailure = {
  id: string;
  filename?: string;
  error: string;
};

export type WorkspaceUploadPromoteResult = {
  uploaded: FileAssetRefDto[];
  promoted: FileAssetRefDto[];
  failed: WorkspaceUploadPromoteFailure[];
};

export class WorkspaceUploadPromoteError extends Error {
  readonly result: WorkspaceUploadPromoteResult;

  constructor(result: WorkspaceUploadPromoteResult) {
    super(formatWorkspaceUploadPromoteMessage(result));
    this.name = "WorkspaceUploadPromoteError";
    this.result = result;
  }
}

export function formatWorkspaceUploadPromoteMessage(
  result: WorkspaceUploadPromoteResult,
): string {
  const uploaded = result.uploaded.length;
  const promoted = result.promoted.length;
  const failed = result.failed.length;
  const failedDetail = result.failed
    .map((item) => `${item.filename ?? item.id}: ${item.error}`)
    .join("; ");

  if (failed === 0) {
    return `Uploaded ${uploaded} file(s); promoted ${promoted}.`;
  }
  if (promoted === 0) {
    return (
      `Uploaded ${uploaded} file(s); promoted 0; failed ${failed} (${failedDetail}). `
      + "No files were added to workspace assets."
    );
  }
  return (
    `Partial success: uploaded ${uploaded}; promoted ${promoted}; failed ${failed} (${failedDetail}). `
    + "Successfully promoted files remain in workspace assets."
  );
}

/**
 * Upload files into the active session scope, then promote each ref into the
 * cross-session workspace asset list (scope=workspace, sessionId IS NULL).
 *
 * Promote runs sequentially. On any promote failure the function still attempts
 * remaining files, then throws {@link WorkspaceUploadPromoteError} with an
 * uploaded/promoted/failed summary so callers never treat partial success as
 * total failure (or silent success).
 */
export async function uploadAndPromoteWorkspaceFiles(
  api: WorkspaceFileUploadApi,
  files: File[],
  sessionId: string | null | undefined,
): Promise<WorkspaceUploadPromoteResult> {
  const trimmed = sessionId?.trim() ?? "";
  if (!trimmed) {
    throw new Error("Select or open a chat session before uploading workspace files.");
  }
  if (files.length === 0) {
    return { uploaded: [], promoted: [], failed: [] };
  }
  const uploadResponse = await api.uploadWorkspaceFiles(files, trimmed);
  const uploaded = uploadResponse.files ?? [];
  const promoted: FileAssetRefDto[] = [];
  const failed: WorkspaceUploadPromoteFailure[] = [];
  for (const file of uploaded) {
    try {
      promoted.push(await api.promoteWorkspaceFile(file.id));
    } catch (error) {
      failed.push({
        id: file.id,
        ...(file.filename ? { filename: file.filename } : {}),
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }
  const result: WorkspaceUploadPromoteResult = { uploaded, promoted, failed };
  if (failed.length > 0) {
    throw new WorkspaceUploadPromoteError(result);
  }
  return result;
}
