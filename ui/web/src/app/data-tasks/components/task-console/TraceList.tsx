import { useMemo, useState } from "react";
import { useT } from "../../../../i18n/locale-context";
import { dataStepKindForTool, dataStepLabel } from "../../data-task-state";
import type { ArtifactDetail, DataArtifact } from "../../data-task-state";
import type { LiveRun } from "../../live-run-state";
import {
  auditStatusLabel,
  buildTraceTimeline,
  type TraceEntry,
} from "../../trace-timeline";
import {
  consoleCodeBlockBaseClass,
  consoleCodeInnerClass,
  consoleScrollXShellClass,
} from "./console-scroll-styles";
import { sectionLabelClass } from "../../ui-tokens";
import { toolResultLooksLikeError } from "../../tool-call-display";
import { ToolFailureResult, ToolFormattedResult } from "../../tool-result-format";
import {
  ArtifactMarkdownPreview,
  isMarkdownFilePath,
} from "./ArtifactMarkdownPreview";

type Translate = ReturnType<typeof useT>;

function entryKindLabel(entry: TraceEntry, t: Translate): string {
  switch (entry.kind) {
    case "run_started":
    case "run_finished":
    case "run_suspended":
    case "run_failed":
      return t("console.traceKindRun");
    case "tool":
      return dataStepLabel(dataStepKindForTool(entry.toolName), t);
    case "artifact":
      return t("console.traceKindOutput");
  }
}

/** Run lifecycle titles come from trace-timeline in English; localize them at render time. */
function entryTitle(entry: TraceEntry, t: Translate): string {
  switch (entry.kind) {
    case "run_started":
      return entry.title === "Run resumed" ? t("console.traceRunResumed") : t("console.traceRunStarted");
    case "run_suspended":
      return t("console.traceRunPaused");
    case "run_finished":
      return t("console.traceRunCompleted");
    case "run_failed":
      return t("console.traceRunFailed");
    default:
      return entry.title;
  }
}

function entryShowsSummary(entry: TraceEntry): boolean {
  return entry.kind === "tool" || entry.kind === "artifact" || entry.kind === "run_failed";
}

function TraceMarker({ entry }: { entry: TraceEntry }) {
  const status =
    entry.kind === "run_failed" || entry.toolStatus === "failed"
      ? "failed"
      : entry.toolStatus === "running"
        ? "running"
        : entry.kind === "run_suspended"
          ? "waiting"
          : entry.kind === "run_started"
            ? "start"
            : "done";
  if (status === "running") {
    return (
      <span className="relative flex h-3.5 w-3.5 items-center justify-center" aria-hidden>
        <span className="absolute h-2.5 w-2.5 animate-ping rounded-full bg-foreground/20" />
        <span className="h-1.5 w-1.5 rounded-full bg-foreground" />
      </span>
    );
  }
  return (
    <span className="flex h-3.5 w-3.5 items-center justify-center" aria-hidden>
      <span
        className={[
          "h-[7px] w-[7px] rounded-full",
          status === "failed"
            ? "bg-danger"
            : status === "waiting"
              ? "bg-warn"
              : status === "start"
                ? "bg-surface ring-[1.5px] ring-border-strong"
                : entry.kind === "artifact"
                  ? "bg-foreground"
                  : "bg-ok",
        ].join(" ")}
      />
    </span>
  );
}

function CompactFilePreview({
  detail,
}: {
  detail: Extract<ArtifactDetail, { type: "file" }>;
}) {
  if (!detail.content) return null;
  const previewText = detail.content.slice(0, 600);
  const truncated = detail.content.length > previewText.length;
  return (
    <div className="mt-2.5 overflow-hidden rounded-lg border border-border">
      <div className="border-b border-border px-3 py-2 font-mono text-[11px] text-muted-light">
        {detail.path}
      </div>
      <div className="p-3">
        {isMarkdownFilePath(detail.path) ? (
          <ArtifactMarkdownPreview content={previewText + (truncated ? "\n\n…" : "")} />
        ) : (
          <pre className={[consoleCodeBlockBaseClass, "max-h-48"].join(" ")}>
            <code className={consoleCodeInnerClass}>
              {previewText}
              {truncated ? "\n…" : ""}
            </code>
          </pre>
        )}
      </div>
    </div>
  );
}

function CompactDatasetPreview({
  detail,
}: {
  detail: Extract<ArtifactDetail, { type: "dataset" }>;
}) {
  const previewRows = detail.rows.slice(0, 5);
  return (
    <div className="mt-2.5 overflow-hidden rounded-lg border border-border">
      <div className="border-b border-border px-3 py-2 font-mono text-[11px] text-muted-light">
        {detail.rows.length.toLocaleString()} × {detail.columns.length}
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-max w-full text-left text-[12px] tabular-nums">
          <thead className="sticky top-0 bg-surface text-muted-light">
            <tr>
              {detail.columns.map((column) => (
                <th key={column} className="whitespace-nowrap px-3 py-1.5 font-medium">
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {previewRows.map((row, rowIndex) => (
              <tr key={rowIndex} className="border-t border-border">
                {row.map((cell, cellIndex) => (
                  <td key={cellIndex} className="whitespace-nowrap px-3 py-1.5 text-muted">
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {detail.rows.length > previewRows.length ? (
        <div className="border-t border-border px-3 py-2 text-[11px] text-muted-light">
          Another {detail.rows.length - previewRows.length} rows are hidden. View the full content in Outputs.
        </div>
      ) : null}
    </div>
  );
}

function resolveFileDetail(
  entry: TraceEntry,
  producedArtifacts: DataArtifact[],
): Extract<ArtifactDetail, { type: "file" }> | undefined {
  if (entry.artifactDetail?.type === "file") {
    return entry.artifactDetail;
  }
  return producedArtifacts.find((artifact) => artifact.detail?.type === "file")
    ?.detail as Extract<ArtifactDetail, { type: "file" }> | undefined;
}

function resolveArtifactDetail(
  entry: TraceEntry,
  producedArtifacts: DataArtifact[],
): Extract<ArtifactDetail, { type: "dataset" }> | undefined {
  if (entry.artifactDetail?.type === "dataset") {
    return entry.artifactDetail;
  }
  return producedArtifacts.find((artifact) => artifact.detail?.type === "dataset")
    ?.detail as Extract<ArtifactDetail, { type: "dataset" }> | undefined;
}

export function TraceEntryCard({
  artifacts,
  entry,
  index,
  isLast = false,
  onSelectArtifact,
  onSelectEvent,
}: {
  artifacts: DataArtifact[];
  entry: TraceEntry;
  index: number;
  isLast?: boolean;
  onSelectArtifact: (artifactId: string) => void;
  onSelectEvent: (eventId: string) => void;
}) {
  const t = useT();
  const [showRawResult, setShowRawResult] = useState(false);
  const producedArtifacts = artifacts.filter((artifact) =>
    entry.artifactIds?.includes(artifact.id),
  );
  const canOpenDetail = entry.eventId !== undefined;
  const datasetDetail = resolveArtifactDetail(entry, producedArtifacts);
  const fileDetail = resolveFileDetail(entry, producedArtifacts);
  const title = entryTitle(entry, t);
  const metaChips = [
    entry.auditStatus ? `Audit ${auditStatusLabel(entry.auditStatus)}` : null,
    entry.scannedRows !== undefined && entry.scannedRows > 0
      ? t("console.scannedRowsMeta", { rows: entry.scannedRows.toLocaleString() })
      : null,
    entry.durationMs !== undefined && entry.durationMs > 0 ? `${entry.durationMs}ms` : null,
    entry.datasourceId ?? null,
  ].filter((chip): chip is string => Boolean(chip));

  return (
    <article className="relative min-w-0 max-w-full pb-4 pl-6" data-trace-index={index + 1}>
      {!isLast ? (
        <span className="absolute bottom-0 left-[6.5px] top-5 w-px bg-border" aria-hidden />
      ) : null}
      <span className="absolute left-0 top-[3px]">
        <TraceMarker entry={entry} />
      </span>
      <div className="flex min-w-0 items-baseline gap-2">
        {canOpenDetail ? (
          <button
            type="button"
            onClick={() => onSelectEvent(entry.eventId!)}
            className="min-w-0 cursor-pointer truncate text-left text-[13px] font-medium text-foreground decoration-border-strong underline-offset-[3px] hover:underline"
          >
            {title}
          </button>
        ) : (
          <h3 className="min-w-0 truncate text-[13px] font-medium text-foreground">{title}</h3>
        )}
        <span className="shrink-0 rounded px-1.5 text-[11px] leading-[18px] text-muted-light ring-1 ring-inset ring-border">
          {entryKindLabel(entry, t)}
        </span>
        <span className="ml-auto shrink-0 font-mono text-[11px] text-muted-light">
          {entry.ts ?? "—"}
        </span>
      </div>

      {entryShowsSummary(entry) && entry.summary ? (
        <p className="mt-0.5 line-clamp-2 break-words text-[12px] leading-5 text-muted">
          {entry.summary}
        </p>
      ) : null}

      {entry.errorMessage ? (
        <div className={[consoleScrollXShellClass, "mt-2"].join(" ")}>
          <p className="min-w-max whitespace-pre rounded-lg bg-danger/8 px-3 py-2 text-xs leading-5 text-danger">
            {entry.errorMessage}
          </p>
        </div>
      ) : null}

      {entry.sql ? (
        <div className="mt-2.5 min-w-0 max-w-full">
          <div className={`mb-1.5 ${sectionLabelClass}`}>
            SQL
          </div>
          <pre className={[consoleCodeBlockBaseClass, "max-h-56"].join(" ")}>
            <code className={consoleCodeInnerClass}>{entry.sql}</code>
          </pre>
        </div>
      ) : null}

      {metaChips.length > 0 ? (
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[11px] text-muted-light">
          {metaChips.map((chip) => (
            <span key={chip}>{chip}</span>
          ))}
        </div>
      ) : null}

      {entry.schemaTables && entry.schemaTables.length > 0 ? (
        <div className="mt-2.5 grid gap-2">
          {entry.schemaTables.map((table) => (
            <div key={table.name} className="rounded-lg border border-border px-3 py-2.5">
              <div className="font-mono text-[12px] font-medium text-foreground">
                {table.name}
              </div>
              {table.description ? (
                <p className="mt-1 text-[11.5px] leading-4 text-muted">
                  {table.description}
                </p>
              ) : null}
              <div className="mt-2 flex flex-wrap gap-1">
                {table.fields.slice(0, 12).map((field) => (
                  <span
                    key={field}
                    className="rounded px-1.5 font-mono text-[11px] leading-[18px] text-muted ring-1 ring-inset ring-border"
                  >
                    {field}
                  </span>
                ))}
                {table.fields.length > 12 ? (
                  <span className="text-[11px] text-muted-light">
                    +{table.fields.length - 12}
                  </span>
                ) : null}
              </div>
            </div>
          ))}
        </div>
      ) : null}

      {datasetDetail ? <CompactDatasetPreview detail={datasetDetail} /> : null}
      {fileDetail ? <CompactFilePreview detail={fileDetail} /> : null}

      {entry.rawResult &&
      !entry.sql &&
      !datasetDetail &&
      !fileDetail &&
      !(entry.schemaTables && entry.schemaTables.length > 0) ? (
        entry.toolName ? (
          <div className="mt-2.5">
            {toolResultLooksLikeError(entry.rawResult) ? (
              <ToolFailureResult toolName={entry.toolName} result={entry.rawResult} />
            ) : (
              <ToolFormattedResult
                toolName={entry.toolName}
                result={entry.rawResult}
                variant="console"
              />
            )}
          </div>
        ) : (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setShowRawResult((current) => !current)}
            className="cursor-pointer text-[12px] text-muted underline decoration-border-strong underline-offset-[3px] hover:text-foreground"
          >
            {showRawResult ? t("console.hideRawResult") : t("console.showRawResult")}
          </button>
          {showRawResult ? (
            <pre className={[consoleCodeBlockBaseClass, "mt-2 max-h-48"].join(" ")}>
              <code className={consoleCodeInnerClass}>{entry.rawResult}</code>
            </pre>
          ) : null}
        </div>
        )
      ) : null}

      {producedArtifacts.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {producedArtifacts.map((artifact) => (
            <button
              key={artifact.id}
              type="button"
              onClick={() => onSelectArtifact(artifact.id)}
              className="inline-flex h-6 max-w-full cursor-pointer items-center gap-1.5 rounded-md bg-surface-hover px-2 text-[11.5px] text-foreground transition-colors duration-150 hover:bg-border"
            >
              <span className="truncate">{artifact.title}</span>
            </button>
          ))}
        </div>
      ) : null}

      {entry.kind === "artifact" && entry.artifactIds?.[0] ? (
        <button
          type="button"
          onClick={() => onSelectArtifact(entry.artifactIds![0])}
          className="mt-2 cursor-pointer text-[12px] text-muted underline decoration-border-strong underline-offset-[3px] hover:text-foreground"
        >
          {t("console.viewOutputDetails")} →
        </button>
      ) : null}
    </article>
  );
}

/**
 * The chronological evidence chain (data footprint) for one run. Shared by the
 * right-panel inline zone and the full-screen TraceOverlay.
 */
export function TraceList({
  artifacts,
  liveRun,
  emptyHint,
  onSelectArtifact,
  onSelectEvent,
}: {
  artifacts: DataArtifact[];
  liveRun: LiveRun;
  emptyHint?: string;
  onSelectArtifact: (artifactId: string) => void;
  onSelectEvent: (eventId: string) => void;
}) {
  const t = useT();
  const entries = useMemo(() => buildTraceTimeline(liveRun), [liveRun]);

  if (entries.length === 0) {
    return (
      <div className="rounded-lg bg-surface-subtle px-5 py-6 text-center">
        <div className="text-[13px] font-medium text-foreground">{t("console.traceEmpty")}</div>
        <p className="mx-auto mt-1.5 max-w-[34ch] text-[12px] leading-5 text-muted-light">
          {emptyHint ?? t("console.traceEmptyHint")}
        </p>
      </div>
    );
  }

  return (
    <div className="grid min-w-0 pt-1">
      {entries.map((entry, index) => (
        <TraceEntryCard
          key={entry.id}
          artifacts={artifacts}
          entry={entry}
          index={index}
          isLast={index === entries.length - 1}
          onSelectArtifact={onSelectArtifact}
          onSelectEvent={onSelectEvent}
        />
      ))}
    </div>
  );
}
