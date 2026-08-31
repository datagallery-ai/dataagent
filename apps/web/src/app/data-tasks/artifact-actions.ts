"use client";

import { useCallback, useState } from "react";
import type { ArtifactExportFormat, JobDto } from "../../lib/config-api";
import { artifactExportClient } from "./artifact-export-client";
import type { DataArtifact } from "./data-task-state";

export type ArtifactExportBusy = "whole" | ArtifactExportFormat | "job" | null;

function triggerBlobDownload(blob: Blob, filename: string): void {
  if (typeof document === "undefined") return;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

/**
 * Single source of truth for artifact download/export. Shared by the outputs card
 * and the expanded artifact page so whole-file download follows one code path.
 */
export function useArtifactExportActions(onExportJob?: (job: JobDto) => void) {
  const [busy, setBusy] = useState<ArtifactExportBusy>(null);
  const [error, setError] = useState<string | null>(null);

  const downloadWhole = useCallback(async (artifact: DataArtifact) => {
    setBusy("whole");
    setError(null);
    try {
      const { blob, filename } = await artifactExportClient.download(artifact.id);
      triggerBlobDownload(blob, filename || artifact.title);
    } catch (err) {
      setError(err instanceof Error ? err.message : "DownloadFailed");
    } finally {
      setBusy(null);
    }
  }, []);

  const downloadFormat = useCallback(
    async (artifact: DataArtifact, format: ArtifactExportFormat) => {
      setBusy(format);
      setError(null);
      try {
        const { blob, filename } = await artifactExportClient.download(artifact.id, format);
        triggerBlobDownload(blob, filename);
      } catch (err) {
        setError(err instanceof Error ? err.message : "DownloadFailed");
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  const exportJob = useCallback(
    async (artifact: DataArtifact, format: ArtifactExportFormat) => {
      setBusy("job");
      setError(null);
      try {
        const job = await artifactExportClient.export(artifact.id, format);
        onExportJob?.(job);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to create export job");
      } finally {
        setBusy(null);
      }
    },
    [onExportJob],
  );

  return { busy, error, setError, downloadWhole, downloadFormat, exportJob };
}

/** Dataset/SQL-style artifacts support format-specific export (CSV/XLSX). */
export function canFormatExport(artifact: DataArtifact): boolean {
  return (
    artifact.type === "dataset" ||
    artifact.type === "sql" ||
    artifact.kind === "csv" ||
    artifact.detail?.type === "dataset"
  );
}
