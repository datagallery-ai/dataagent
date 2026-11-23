"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useT } from "../../../../i18n/locale-context";
import { configApi } from "../../../../lib/config-api";
import type { FileAssetRefDto } from "../../../../lib/config-api";
import { filterWorkspaceAssetFiles, hasCapability } from "../../data-task-state";
import {
  uploadAndPromoteWorkspaceFiles,
  WorkspaceUploadPromoteError,
} from "../../workspace-file-upload";
import { btnSecondaryClass, panelShellClass, panelTitleClass, sectionLabelClass } from "../../ui-tokens";

function formatBytes(bytes?: number): string {
  if (bytes === undefined || !Number.isFinite(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

type WorkspaceFileAssetsPanelProps = {
  /** Active chat session / thread id — required for session-scoped upload + promote. */
  sessionId?: string | null;
  onFilesChange?: (files: FileAssetRefDto[]) => void;
};

export function WorkspaceFileAssetsPanel({
  sessionId,
  onFilesChange,
}: WorkspaceFileAssetsPanelProps) {
  const t = useT();
  const [files, setFiles] = useState<FileAssetRefDto[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const canUpload = Boolean(sessionId?.trim());

  const refresh = useCallback(async (options?: { clearError?: boolean }) => {
    if (!hasCapability("files")) {
      setFiles([]);
      onFilesChange?.([]);
      return;
    }
    setLoading(true);
    if (options?.clearError !== false) {
      setError(null);
    }
    try {
      const response = await configApi.listWorkspaceFiles({
        scope: "workspace",
        origin: ["uploaded", "saved"],
      });
      const workspaceFiles = filterWorkspaceAssetFiles(response.files ?? []);
      setFiles(workspaceFiles);
      onFilesChange?.(workspaceFiles);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("assets.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [onFilesChange, t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (!hasCapability("files")) {
    return null;
  }

  const handleUpload = async (selected: FileList | null) => {
    if (!selected?.length) return;
    if (!canUpload) {
      setError(t("assets.needSessionError"));
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await uploadAndPromoteWorkspaceFiles(configApi, [...selected], sessionId);
      await refresh();
    } catch (err) {
      if (err instanceof WorkspaceUploadPromoteError) {
        setError(err.message);
        if (err.result.promoted.length > 0) {
          // Keep the partial-success message visible while refreshing the list.
          await refresh({ clearError: false });
        }
      } else {
        setError(err instanceof Error ? err.message : t("assets.uploadFailed"));
      }
    } finally {
      setLoading(false);
      if (inputRef.current) {
        inputRef.current.value = "";
      }
    }
  };

  const handleDownload = async (file: FileAssetRefDto) => {
    try {
      const { blob, filename } = await configApi.downloadWorkspaceFile(file.id);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename || file.filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("assets.downloadFailed"));
    }
  };

  const handleDelete = async (file: FileAssetRefDto) => {
    const confirmed = window.confirm(t("assets.deleteConfirm", { name: file.filename }));
    if (!confirmed) return;
    setLoading(true);
    setError(null);
    try {
      await configApi.deleteWorkspaceFile(file.id);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("assets.deleteFailed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className={panelShellClass}>
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className={panelTitleClass}>{t("assets.panelTitle")}</h3>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className={`${btnSecondaryClass} disabled:opacity-60`}
          >
            {t("common.refresh")}
          </button>
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            disabled={loading || !canUpload}
            title={canUpload ? undefined : t("assets.needSessionTitle")}
            className={`${btnSecondaryClass} disabled:opacity-60`}
          >
            {t("common.upload")}
          </button>
          <input
            ref={inputRef}
            type="file"
            multiple
            className="hidden"
            disabled={!canUpload}
            onChange={(event) => void handleUpload(event.target.files)}
          />
        </div>
      </div>
      <p className="mb-3 text-[11px] leading-4 text-muted-light">
        {t("assets.panelHelp")}
      </p>
      {!canUpload ? (
        <p className="mb-3 text-[11px] leading-4 text-muted-light">
          {t("assets.needSession")}
        </p>
      ) : null}
      {error ? (
        <p className="mb-3 rounded-lg bg-step-error/10 px-2.5 py-2 text-xs text-step-error">
          {error}
        </p>
      ) : null}
      {loading && files.length === 0 ? (
        <p className="text-xs text-muted-light">{t("assets.loading")}</p>
      ) : files.length === 0 ? (
        <p className="text-xs text-muted-light">{t("assets.empty")}</p>
      ) : (
        <div className="grid gap-2">
          {files.map((file) => (
            <div
              key={file.id}
              className="flex items-center justify-between gap-3 rounded-lg border border-border bg-surface-subtle px-3 py-2"
            >
              <div className="min-w-0">
                <div className="truncate text-xs font-semibold text-foreground">
                  {file.filename}
                </div>
                <div className="mt-0.5 flex flex-wrap gap-2 text-[10px] text-muted-light">
                  <span>{formatBytes(file.sizeBytes)}</span>
                  {file.mimeType ? <span>{file.mimeType}</span> : null}
                  {file.source ? <span>{file.source}</span> : null}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <button
                  type="button"
                  onClick={() => void handleDownload(file)}
                  className={btnSecondaryClass}
                >
                  {t("common.download")}
                </button>
                <button
                  type="button"
                  onClick={() => void handleDelete(file)}
                  disabled={loading}
                  className={`${btnSecondaryClass} text-step-error hover:border-step-error/40 hover:text-step-error disabled:opacity-60`}
                >
                  {t("common.delete")}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="mt-2">
        <span className={sectionLabelClass}>{t("assets.fileCount", { count: files.length })}</span>
      </div>
    </section>
  );
}
