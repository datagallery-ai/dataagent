"use client";

import { useCallback, useEffect, useState } from "react";
import type { EvidenceRef } from "@datafoundry/contracts";
import { useT } from "../../../../i18n/locale-context";
import { CONSOLE_PEER_PAGE_ID, shouldRevealConsoleForSelection } from "../../task-console-layout";
import { artifactToneForType } from "../../ui-tokens";
import { TaskConsole, type TaskConsoleProps } from "./TaskConsole";
import { ExpandedArtifactView } from "./ExpandedArtifactView";
import { ArtifactPreviewModal } from "./ArtifactPreviewModal";

type TaskConsolePanelProps = Omit<
  TaskConsoleProps,
  "onOpenArtifactPage" | "onPreviewArtifact"
> & {
  sessionId: string;
  runId?: string;
  onReferenceEvidence: (ref: EvidenceRef) => void;
};

/**
 * Right-panel shell that hosts the Task Console and any number of expanded artifact
 * pages as sibling tabs. The console tab is fixed; each opened artifact becomes its
 * own closable page peer to the console. Preview opens a full-page overlay modal.
 */
export function TaskConsolePanel({
  sessionId,
  runId,
  onReferenceEvidence,
  selection,
  ...consoleProps
}: TaskConsolePanelProps) {
  const t = useT();
  const { artifacts } = consoleProps;
  const { artifactFocusId, onArtifactFocusHandled } = consoleProps;
  const [openArtifactIds, setOpenArtifactIds] = useState<string[]>([]);
  const [activePageId, setActivePageId] = useState<string>(CONSOLE_PEER_PAGE_ID);
  const [previewArtifactId, setPreviewArtifactId] = useState<string | null>(null);

  // Drop pages whose artifact is gone (e.g. after switching sessions/runs).
  useEffect(() => {
    const available = new Set(artifacts.map((artifact) => artifact.id));
    setOpenArtifactIds((current) => {
      const next = current.filter((id) => available.has(id));
      return next.length === current.length ? current : next;
    });
  }, [artifacts]);

  useEffect(() => {
    if (activePageId === CONSOLE_PEER_PAGE_ID) return;
    if (!artifacts.some((artifact) => artifact.id === activePageId)) {
      setActivePageId(CONSOLE_PEER_PAGE_ID);
    }
  }, [activePageId, artifacts]);

  // Chat tool chips / process steps select Details — leave artifact peer pages and
  // dismiss preview overlay so the console (and its Details tab) is actually visible.
  useEffect(() => {
    if (shouldRevealConsoleForSelection(selection)) {
      setActivePageId(CONSOLE_PEER_PAGE_ID);
      setPreviewArtifactId(null);
    }
  }, [selection]);

  const openArtifactPage = useCallback((artifactId: string) => {
    setOpenArtifactIds((current) =>
      current.includes(artifactId) ? current : [...current, artifactId],
    );
    setActivePageId(artifactId);
    // Peer page replaces the floating preview — don't leave the overlay on top.
    setPreviewArtifactId(null);
  }, []);

  const openArtifactPreview = useCallback((artifactId: string) => {
    setPreviewArtifactId(artifactId);
  }, []);

  // Drop preview when the artifact disappears (session/run switch).
  useEffect(() => {
    if (!previewArtifactId) return;
    if (!artifacts.some((artifact) => artifact.id === previewArtifactId)) {
      setPreviewArtifactId(null);
    }
  }, [artifacts, previewArtifactId]);

  // A focus request (e.g. from the Trace overlay) opens the artifact's peer page.
  useEffect(() => {
    if (!artifactFocusId) return;
    if (artifacts.some((artifact) => artifact.id === artifactFocusId)) {
      openArtifactPage(artifactFocusId);
    }
    onArtifactFocusHandled?.();
  }, [artifactFocusId, artifacts, onArtifactFocusHandled, openArtifactPage]);

  const closeArtifactPage = useCallback(
    (artifactId: string) => {
      setOpenArtifactIds((current) => current.filter((id) => id !== artifactId));
      setActivePageId((current) =>
        current === artifactId ? CONSOLE_PEER_PAGE_ID : current,
      );
    },
    [],
  );

  // Esc ladder inside the panel: preview is handled by ArtifactPreviewModal (capture).
  // On an artifact peer page, Esc returns to the console tab (does not close the panel).
  useEffect(() => {
    if (previewArtifactId) return;
    if (activePageId === CONSOLE_PEER_PAGE_ID) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      setActivePageId(CONSOLE_PEER_PAGE_ID);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [activePageId, previewArtifactId]);

  const activeArtifact =
    activePageId === CONSOLE_PEER_PAGE_ID
      ? null
      : artifacts.find((artifact) => artifact.id === activePageId) ?? null;

  const previewArtifact = previewArtifactId
    ? artifacts.find((artifact) => artifact.id === previewArtifactId) ?? null
    : null;

  const hasPages = openArtifactIds.length > 0;

  return (
    <div className="flex h-full min-h-0 w-full flex-1 flex-col overflow-hidden">
      {hasPages ? (
        <nav className="flex shrink-0 items-stretch gap-1 overflow-x-auto border-b border-l border-border bg-surface px-2 py-1.5">
          <PageTab
            label={t("console.pageConsole")}
            active={activePageId === CONSOLE_PEER_PAGE_ID}
            onSelect={() => setActivePageId(CONSOLE_PEER_PAGE_ID)}
          />
          {openArtifactIds.map((id) => {
            const artifact = artifacts.find((entry) => entry.id === id);
            if (!artifact) return null;
            const tone = artifactToneForType(artifact.type ?? artifact.kind);
            return (
              <PageTab
                key={id}
                label={artifact.title}
                icon={tone.icon}
                active={activePageId === id}
                onSelect={() => setActivePageId(id)}
                onClose={() => closeArtifactPage(id)}
                closeAriaLabel={t("console.closePage", { title: artifact.title })}
              />
            );
          })}
        </nav>
      ) : null}

      <div className="min-h-0 flex-1 overflow-hidden">
        {activeArtifact ? (
          <div className="h-full border-l border-border bg-surface">
            <ExpandedArtifactView
              key={activeArtifact.id}
              artifact={activeArtifact}
              sessionId={sessionId}
              runId={runId}
              onReferenceEvidence={onReferenceEvidence}
              onArtifactExportJob={consoleProps.onArtifactExportJob}
            />
          </div>
        ) : (
          <TaskConsole
            {...consoleProps}
            selection={selection}
            sessionId={sessionId}
            onOpenArtifactPage={openArtifactPage}
            onPreviewArtifact={openArtifactPreview}
          />
        )}
      </div>

      {previewArtifact ? (
        <ArtifactPreviewModal
          artifact={previewArtifact}
          sessionId={sessionId}
          runId={runId}
          onClose={() => setPreviewArtifactId(null)}
          onOpenPage={openArtifactPage}
          onReferenceEvidence={onReferenceEvidence}
          onArtifactExportJob={consoleProps.onArtifactExportJob}
        />
      ) : null}
    </div>
  );
}

function PageTab({
  label,
  icon,
  active,
  onSelect,
  onClose,
  closeAriaLabel,
}: {
  label: string;
  icon?: string;
  active: boolean;
  onSelect: () => void;
  onClose?: () => void;
  closeAriaLabel?: string;
}) {
  return (
    <div
      className={[
        "flex max-w-[180px] shrink-0 items-center gap-1 rounded-lg px-2.5 py-1 text-xs font-semibold transition-colors",
        active ? "bg-primary text-white" : "text-muted hover:bg-surface-subtle hover:text-foreground",
      ].join(" ")}
    >
      <button
        type="button"
        onClick={onSelect}
        className="flex min-w-0 cursor-pointer items-center gap-1.5"
      >
        {icon ? <span className="shrink-0 text-[10px] leading-none">{icon}</span> : null}
        <span className="min-w-0 truncate">{label}</span>
      </button>
      {onClose ? (
        <button
          type="button"
          aria-label={closeAriaLabel ?? label}
          onClick={(event) => {
            event.stopPropagation();
            onClose();
          }}
          className={[
            "grid h-4 w-4 shrink-0 place-items-center rounded transition-colors",
            active ? "text-white/70 hover:bg-white/20" : "text-muted-light hover:bg-surface",
          ].join(" ")}
        >
          <span aria-hidden="true" className="text-sm leading-none">
            ×
          </span>
        </button>
      ) : null}
    </div>
  );
}
