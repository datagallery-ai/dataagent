"use client";

import { useEffect, useMemo, useState } from "react";
import type { EvidenceRef, EvidenceSelection } from "@datafoundry/contracts";
import type { ArtifactDetail, DataArtifact } from "../../data-task-state";
import { hasCapability } from "../../data-task-state";
import { artifactExportClient } from "../../artifact-export-client";
import {
  artifactDetailFromPreview,
  artifactDetailNeedsPreviewFetch,
  mergeArtifactDetail,
} from "../../live-run-state";
import { artifactEvidenceRef } from "../../evidence";
import { canFormatExport, useArtifactExportActions } from "../../artifact-actions";
import type { JobDto } from "../../../../lib/config-api";
import {
  btnPrimaryClass,
  btnSecondaryClass,
  artifactToneForType,
  sectionLabelClass,
} from "../../ui-tokens";
import {
  consoleCodeBlockBaseClass,
  consoleCodeInnerClass,
  consoleScrollXShellClass,
  consoleTableShellClass,
} from "./console-scroll-styles";
import { ChartDetailView, FileDetailView } from "./TaskConsole";
import { ArtifactMarkdownPreview } from "./ArtifactMarkdownPreview";
import { SelectableDataGrid, SelectableText } from "./evidence-selection";
import { ActionMenu, type ActionMenuItem } from "./ActionMenu";
import { IconSelection } from "./console-icons";
import { useT } from "../../../../i18n/locale-context";

/** Resolves the full artifact detail, fetching preview data lazily when required. */
function useResolvedArtifactDetail(artifact: DataArtifact): {
  detail: ArtifactDetail | undefined;
  loading: boolean;
  error: string | null;
} {
  const t = useT();
  const exportReady = hasCapability("artifact.export");
  const [detail, setDetail] = useState<ArtifactDetail | undefined>(artifact.detail);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(artifact.detail);
    setError(null);
  }, [artifact.detail, artifact.id]);

  useEffect(() => {
    if (!exportReady || !artifactDetailNeedsPreviewFetch(artifact, detail)) {
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    void artifactExportClient
      .fetchPreview(artifact.id)
      .then((preview) => {
        if (cancelled) return;
        const loaded = artifactDetailFromPreview(artifact, preview);
        if (loaded) {
          setDetail((current) => mergeArtifactDetail(current, loaded));
          return;
        }
        setError(t("console.previewUnsupported"));
      })
      .catch((fetchError: unknown) => {
        if (cancelled) return;
        setError(
          fetchError instanceof Error ? fetchError.message : t("console.failedToLoadPreview"),
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [artifact, detail, exportReady, t]);

  return { detail, loading, error };
}

/** A stable id suffix so distinct selections from the same artifact coexist as chips. */
function selectionKey(selection: EvidenceSelection): string {
  if (selection.mode === "text") {
    let hash = 0;
    for (let i = 0; i < selection.quote.length; i += 1) {
      hash = (hash * 31 + selection.quote.charCodeAt(i)) | 0;
    }
    return `text:${hash}`;
  }
  const { range } = selection;
  return `${selection.mode}:${range.r0},${range.c0},${range.r1},${range.c1}`;
}

/**
 * Full-panel, immersive view for a single artifact. On the peer cite page
 * (`presentation="page"`), supports fine-grained selection (table regions,
 * highlighted text) as evidence references, plus whole-artifact cite and download.
 * In preview modal (`presentation="modal"`), selection cite is disabled; whole-artifact
 * cite remains available (use header “Cite” to open the side panel for partial cites).
 */
export function ExpandedArtifactView({
  artifact,
  sessionId,
  runId,
  onReferenceEvidence,
  onArtifactExportJob,
  presentation = "page",
}: {
  artifact: DataArtifact;
  sessionId: string;
  runId?: string;
  onReferenceEvidence: (ref: EvidenceRef) => void;
  onArtifactExportJob?: (job: JobDto) => void;
  /** `modal` disables selection cite and hides the peer-page selection hint. */
  presentation?: "page" | "modal";
}) {
  const t = useT();
  const { detail, loading, error } = useResolvedArtifactDetail(artifact);
  const baseRef = useMemo(
    () => artifactEvidenceRef(artifact, sessionId, runId),
    [artifact, sessionId, runId],
  );
  const tone = artifactToneForType(artifact.type ?? artifact.kind);
  const exportReady = hasCapability("artifact.export");
  const formatExport = canFormatExport(artifact);
  const { busy, error: downloadError, downloadWhole, downloadFormat, exportJob } =
    useArtifactExportActions(onArtifactExportJob);
  const downloadBusy = busy !== null;
  const [referenceFlash, setReferenceFlash] = useState(false);
  const selectionEnabled = presentation === "page";

  useEffect(() => {
    if (!referenceFlash) return;
    const timer = window.setTimeout(() => setReferenceFlash(false), 2200);
    return () => window.clearTimeout(timer);
  }, [referenceFlash]);

  const notifyReferenced = () => setReferenceFlash(true);

  const referenceWhole = () => {
    onReferenceEvidence(baseRef);
    notifyReferenced();
  };
  const referenceSelection = (selection: EvidenceSelection) => {
    onReferenceEvidence({
      ...baseRef,
      id: `${baseRef.id}:sel:${selectionKey(selection)}`,
      source: { ...baseRef.source, selection },
    });
    notifyReferenced();
  };

  const downloadItems: ActionMenuItem[] = [
    {
      key: "whole",
      label: busy === "whole" ? t("console.downloadingEllipsis") : t("console.downloadFile"),
      disabled: downloadBusy,
      onSelect: () => void downloadWhole(artifact),
    },
    {
      key: "csv",
      label: busy === "csv" ? t("console.preparingCsv") : t("console.downloadCsv"),
      disabled: downloadBusy,
      onSelect: () => void downloadFormat(artifact, "csv"),
    },
    {
      key: "xlsx",
      label: busy === "xlsx" ? t("console.preparingXlsx") : t("console.downloadXlsx"),
      disabled: downloadBusy,
      onSelect: () => void downloadFormat(artifact, "xlsx"),
    },
    {
      key: "job",
      label: busy === "job" ? t("console.submitting") : t("console.backgroundExportXlsx"),
      disabled: downloadBusy,
      onSelect: () => void exportJob(artifact, "xlsx"),
    },
  ];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 flex-col gap-2 border-b border-border bg-surface px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <span
              className={[
                "inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold",
                tone.bg,
                tone.border,
                tone.text,
              ].join(" ")}
            >
              <span className="text-[10px] leading-none">{tone.icon}</span>
              {artifact.type ?? artifact.kind}
            </span>
            <h3 className="min-w-0 truncate text-sm font-semibold text-foreground" title={artifact.title}>
              {artifact.title}
            </h3>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button type="button" onClick={referenceWhole} className={btnPrimaryClass}>
              {t("console.referenceWhole")}
            </button>
            {exportReady ? (
              formatExport ? (
                <ActionMenu
                  items={downloadItems}
                  triggerClass={`inline-flex items-center gap-1 ${btnSecondaryClass}`}
                  triggerLabel={t("console.download")}
                  ariaLabel={t("console.downloadOutput")}
                />
              ) : (
                <button
                  type="button"
                  onClick={() => void downloadWhole(artifact)}
                  disabled={downloadBusy}
                  className={`${btnSecondaryClass} disabled:cursor-not-allowed disabled:opacity-60`}
                  title={
                    downloadBusy
                      ? t("console.downloadingEllipsis")
                      : t("console.downloadOutputFile")
                  }
                >
                  {busy === "whole" ? t("console.downloading") : t("console.download")}
                </button>
              )
            ) : null}
          </div>
        </div>
        {selectionEnabled ? (
          <div className="flex items-center gap-1.5 text-[11px] leading-4 text-muted-light">
            <IconSelection className="h-3.5 w-3.5 shrink-0" />
            {t("console.referenceWholeHint")}
          </div>
        ) : null}
        {referenceFlash ? (
          <p
            role="status"
            className="rounded-md border border-primary/20 bg-primary/8 px-2.5 py-1.5 text-[11px] font-medium text-primary"
          >
            {t("console.referenced")}
          </p>
        ) : null}
      </header>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 p-4">
        {artifact.summary ? (
          <p className="text-xs leading-5 text-muted">{artifact.summary}</p>
        ) : null}
        {downloadError ? (
          <p className="rounded-lg bg-step-error/10 px-2.5 py-2 text-xs text-step-error">
            {downloadError}
          </p>
        ) : null}
        <div className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden rounded-xl border border-border bg-surface p-4">
          <ExpandedArtifactBody
            artifact={artifact}
            detail={detail}
            loading={loading}
            error={error}
            onReferenceSelection={selectionEnabled ? referenceSelection : undefined}
          />
        </div>
      </div>
    </div>
  );
}

function ExpandedArtifactBody({
  artifact,
  detail,
  loading,
  error,
  onReferenceSelection,
}: {
  artifact: DataArtifact;
  detail: ArtifactDetail | undefined;
  loading: boolean;
  error: string | null;
  onReferenceSelection?: (selection: EvidenceSelection) => void;
}) {
  const t = useT();

  if (!detail) {
    if (loading) {
      return <p className="text-xs text-muted-light">{t("console.loadingPreview")}</p>;
    }
    if (error) {
      return (
        <p className="rounded-lg bg-step-error/10 px-2.5 py-2 text-xs text-step-error">{error}</p>
      );
    }
    return (
      <p className="rounded-lg border border-dashed border-border bg-surface-subtle px-3 py-4 text-xs leading-5 text-muted-light">
        {t("console.noViewableDetails")}
      </p>
    );
  }

  if (detail.type === "dataset") {
    if (onReferenceSelection) {
      return (
        <SelectableDataGrid
          columns={detail.columns}
          rows={detail.rows}
          onReference={onReferenceSelection}
        />
      );
    }
    return <ReadOnlyDataGrid columns={detail.columns} rows={detail.rows} />;
  }

  if (detail.type === "chart") {
    const grid = chartToGrid(detail);
    return (
      <div className="grid min-w-0 gap-3">
        <ChartDetailView detail={detail} />
        {grid.rows.length > 0 ? (
          <div className="grid gap-1.5">
            <div className={sectionLabelClass}>
              {onReferenceSelection
                ? t("console.dataPointsSelect")
                : t("console.dataPoints")}
            </div>
            {onReferenceSelection ? (
              <SelectableDataGrid
                columns={grid.columns}
                rows={grid.rows}
                onReference={onReferenceSelection}
              />
            ) : (
              <ReadOnlyDataGrid columns={grid.columns} rows={grid.rows} />
            )}
          </div>
        ) : null}
      </div>
    );
  }

  if (detail.type === "sql") {
    const sqlCode = (
      <div className={consoleScrollXShellClass}>
        <pre className={[consoleCodeBlockBaseClass, "max-h-[60vh]"].join(" ")}>
          <code className={consoleCodeInnerClass}>{detail.sql}</code>
        </pre>
      </div>
    );
    return (
      <div className="grid min-w-0 gap-3">
        {onReferenceSelection ? (
          <SelectableText onReference={onReferenceSelection}>{sqlCode}</SelectableText>
        ) : (
          sqlCode
        )}
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
          <span>
            {t("console.scannedRowsMeta", { rows: detail.scannedRows.toLocaleString() })}
          </span>
          <span>·</span>
          <span>{detail.durationMs}ms</span>
        </div>
      </div>
    );
  }

  if (detail.type === "file") {
    const fileView = <FileDetailView detail={detail} artifact={artifact} bare />;
    return onReferenceSelection ? (
      <SelectableText onReference={onReferenceSelection}>{fileView}</SelectableText>
    ) : (
      fileView
    );
  }

  const reportView = (
    <div className="grid min-w-0 gap-4">
      {detail.sections.map((section) => (
        <div key={section.heading} className="min-w-0">
          <div className={sectionLabelClass}>{section.heading}</div>
          <div className="mt-1 min-w-0 text-xs leading-5 text-muted">
            <ArtifactMarkdownPreview content={section.body} bare />
          </div>
        </div>
      ))}
    </div>
  );
  return onReferenceSelection ? (
    <SelectableText onReference={onReferenceSelection}>{reportView}</SelectableText>
  ) : (
    reportView
  );
}

/** Plain table for preview modal — no drag-select / floating cite chrome. */
function ReadOnlyDataGrid({
  columns,
  rows,
}: {
  columns: string[];
  rows: string[][];
}) {
  return (
    <div className={consoleTableShellClass}>
      <div className="max-h-[min(560px,64vh)] overflow-auto">
        <table className="min-w-max w-full text-left text-xs">
          <thead className="sticky top-0 z-10 bg-surface-subtle text-muted-light shadow-[0_1px_0_0_var(--border)]">
            <tr>
              {columns.map((column, columnIndex) => (
                <th
                  key={`${column}-${columnIndex}`}
                  className="whitespace-nowrap px-3 py-2 font-semibold"
                >
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td
                  colSpan={Math.max(columns.length, 1)}
                  className="border-t border-border px-3 py-6 text-center text-muted-light"
                >
                  —
                </td>
              </tr>
            ) : (
              rows.map((row, rowIndex) => (
                <tr key={rowIndex} className="border-t border-border">
                  {row.map((cell, cellIndex) => (
                    <td
                      key={cellIndex}
                      className={[
                        "whitespace-nowrap px-3 py-2",
                        cellIndex === 0 ? "font-medium text-foreground" : "text-muted",
                      ].join(" ")}
                    >
                      {cell}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Flattens chart points/series into a selectable table so parts can be referenced. */
function chartToGrid(detail: Extract<ArtifactDetail, { type: "chart" }>): {
  columns: string[];
  rows: string[][];
} {
  if (detail.series && detail.series.length > 0) {
    return {
      columns: ["series", "label", "value"],
      rows: detail.series.flatMap((series) =>
        series.points.map((point) => [series.name, point.label, String(point.value)]),
      ),
    };
  }
  return {
    columns: ["label", "value"],
    rows: detail.points.map((point) => [point.label, String(point.value)]),
  };
}
