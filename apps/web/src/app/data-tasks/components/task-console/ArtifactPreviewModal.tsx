"use client";

import { useEffect } from "react";
import { createPortal } from "react-dom";
import type { EvidenceRef } from "@datafoundry/contracts";
import { useT } from "../../../../i18n/locale-context";
import type { DataArtifact } from "../../data-task-state";
import type { JobDto } from "../../../../lib/config-api";
import {
  btnPrimaryClass,
  btnSecondaryClass,
  overlayPanelClass,
} from "../../ui-tokens";
import { ExpandedArtifactView } from "./ExpandedArtifactView";

type ArtifactPreviewModalProps = {
  artifact: DataArtifact;
  sessionId: string;
  runId?: string;
  onClose: () => void;
  onOpenPage: (artifactId: string) => void;
  onReferenceEvidence: (ref: EvidenceRef) => void;
  onArtifactExportJob?: (job: JobDto) => void;
};

/**
 * Full-viewport overlay for quickly reading an output without leaving the current
 * layout. Reuses ExpandedArtifactView for type-specific preview (no selection cite
 * in modal). Header “Cite” closes the preview and opens the right-side cite page.
 */
export function ArtifactPreviewModal({
  artifact,
  sessionId,
  runId,
  onClose,
  onOpenPage,
  onReferenceEvidence,
  onArtifactExportJob,
}: ArtifactPreviewModalProps) {
  const t = useT();

  useEffect(() => {
    // Capture + stopImmediatePropagation so Esc closes only the preview —
    // not the parent console drawer (which also listens for Escape).
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopImmediatePropagation();
      onClose();
    };
    window.addEventListener("keydown", onKeyDown, true);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      document.body.style.overflow = previousOverflow;
    };
  }, [onClose]);

  if (typeof document === "undefined") return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[60] bg-foreground/40 p-2 backdrop-blur-sm sm:p-4"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("console.previewDialogLabel", { title: artifact.title })}
        className={`mx-auto h-full max-w-5xl ${overlayPanelClass}`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-light">
              {t("console.preview")}
            </p>
            <h2 className="mt-0.5 truncate text-sm font-semibold text-foreground" title={artifact.title}>
              {artifact.title}
            </h2>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              className={btnSecondaryClass}
              onClick={() => {
                onOpenPage(artifact.id);
                onClose();
              }}
              title={t("console.citeTitle")}
            >
              {t("console.cite")}
            </button>
            <button
              type="button"
              className={btnPrimaryClass}
              onClick={onClose}
              aria-label={t("common.close")}
            >
              {t("common.close")}
            </button>
          </div>
        </header>
        <div className="min-h-0 flex-1 overflow-hidden">
          <ExpandedArtifactView
            key={artifact.id}
            artifact={artifact}
            sessionId={sessionId}
            runId={runId}
            onReferenceEvidence={onReferenceEvidence}
            onArtifactExportJob={onArtifactExportJob}
            presentation="modal"
          />
        </div>
      </div>
    </div>,
    document.body,
  );
}
