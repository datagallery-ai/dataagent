"use client";

import {
  PER_RUN_MENTION_APPEARANCE,
  SESSION_HEADER_RESOURCE_KINDS,
  SESSION_RESOURCE_LABEL,
  isSessionResourceKindLocked,
  sessionEnabledItems,
  sessionResourceCounts,
  type ChatSession,
  type PerRunMentionKind,
  type SessionStartedHints,
  type WorkspaceConfigItem,
  type WorkspaceConfigStore,
} from "../../data-task-state";

type SessionResourceSummaryProps = {
  workspaceConfig: WorkspaceConfigStore;
  session: ChatSession | null;
  sessionStartedHints?: SessionStartedHints;
  kinds?: PerRunMentionKind[];
  interactive?: boolean;
  openKind?: PerRunMentionKind | null;
  onToggleOpen?: (kind: PerRunMentionKind) => void;
};

export function SessionResourceSummary({
  workspaceConfig,
  session,
  sessionStartedHints,
  kinds = SESSION_HEADER_RESOURCE_KINDS,
  interactive = false,
  openKind = null,
  onToggleOpen,
}: SessionResourceSummaryProps) {
  return (
    <div
      className="flex min-w-0 flex-wrap items-center gap-1.5"
      data-testid="session-resource-summary"
    >
      {kinds.map((kind) => {
        const counts = sessionResourceCounts(workspaceConfig, kind, session);
        const locked = isSessionResourceKindLocked(
          session,
          kind,
          sessionStartedHints,
        );
        const appearance = PER_RUN_MENTION_APPEARANCE[kind];
        const label = SESSION_RESOURCE_LABEL[kind];
        const open = openKind === kind;

        const pillClass = [
          "inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium",
          open ? appearance.pillOpen : appearance.pill,
          locked ? "opacity-90" : "",
        ].join(" ");

        const content = (
          <>
            {interactive ? <ChevronUpIcon open={open} /> : null}
            <span
              className={[
                "inline-flex h-4 w-4 items-center justify-center rounded-md",
                appearance.badge,
              ].join(" ")}
              aria-hidden
            >
              <ResourceKindIcon kind={kind} className="h-3 w-3" />
            </span>
            <span className="opacity-70 tabular-nums">
              {counts.enabled}/{counts.total}
            </span>
            {locked ? <LockIcon /> : null}
          </>
        );

        if (interactive && onToggleOpen) {
          return (
            <button
              key={kind}
              type="button"
              aria-haspopup="listbox"
              aria-expanded={open}
              aria-label={`${label} session settings`}
              onClick={() => onToggleOpen(kind)}
              className={pillClass}
            >
              {content}
            </button>
          );
        }

        return (
          <span
            key={kind}
            className={pillClass}
            aria-label={`${label} ${counts.enabled} of ${counts.total} enabled${
              locked ? ", locked" : ""
            }`}
          >
            {content}
          </span>
        );
      })}
    </div>
  );
}

export function SessionHeaderResourceChips({
  workspaceConfig,
  session,
  onPreviewDatasource,
}: {
  workspaceConfig: WorkspaceConfigStore;
  session: ChatSession | null;
  onPreviewDatasource?: (itemId: string) => void;
}) {
  const resources = SESSION_HEADER_RESOURCE_KINDS.flatMap((kind) =>
    sessionEnabledItems(workspaceConfig, kind, session).map((item) => ({
      kind,
      item,
    })),
  );

  if (resources.length === 0) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-0.5 text-[11px] font-medium text-muted shadow-[var(--shadow-card)]">
        <ResourceKindIcon kind="db" className="h-3.5 w-3.5 shrink-0" />
        <span>No db/kb selected</span>
      </span>
    );
  }

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      {resources.map(({ kind, item }) => (
        <HeaderResourceChip
          key={`${kind}:${item.id}`}
          kind={kind}
          item={item}
          onPreviewDatasource={onPreviewDatasource}
        />
      ))}
    </div>
  );
}

function HeaderResourceChip({
  kind,
  item,
  onPreviewDatasource,
}: {
  kind: PerRunMentionKind;
  item: WorkspaceConfigItem;
  onPreviewDatasource?: (itemId: string) => void;
}) {
  const label = SESSION_RESOURCE_LABEL[kind];
  const className =
    "inline-flex max-w-[220px] items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-0.5 text-[11px] font-medium text-muted shadow-[var(--shadow-card)]";
  const content = (
    <>
      <ResourceKindIcon kind={kind} className="h-3.5 w-3.5 shrink-0" />
      <span className="truncate">{item.name || item.id}</span>
    </>
  );

  if (kind === "db" && onPreviewDatasource) {
    return (
      <button
        type="button"
        onClick={() => onPreviewDatasource(item.id)}
        className={`${className} cursor-pointer text-left transition-colors duration-200 hover:border-primary-light/40 hover:text-foreground`}
        title={`Preview ${label}: ${item.name || item.id}`}
      >
        {content}
      </button>
    );
  }

  return (
    <span
      className={className}
      title={`${label}: ${item.name || item.id}`}
    >
      {content}
    </span>
  );
}

export function ResourceKindIcon({
  kind,
  className,
}: {
  kind: PerRunMentionKind;
  className?: string;
}) {
  if (kind === "db") {
    return (
      <svg
        viewBox="0 0 24 24"
        className={className}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <ellipse cx="12" cy="5" rx="7" ry="3" />
        <path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5" />
        <path d="M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" />
      </svg>
    );
  }

  if (kind === "kb") {
    return (
      <svg
        viewBox="0 0 24 24"
        className={className}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v16H7a3 3 0 0 0-3 3V5.5Z" />
        <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
      </svg>
    );
  }

  if (kind === "mcp") {
    return (
      <svg
        viewBox="0 0 24 24"
        className={className}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <path d="M14.5 6.5 17 4l3 3-2.5 2.5" />
        <path d="m3 21 8.5-8.5" />
        <path d="M12 7a5 5 0 0 0 5 5" />
      </svg>
    );
  }

  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M5 7.5A3.5 3.5 0 0 1 8.5 4h7A3.5 3.5 0 0 1 19 7.5v9a3.5 3.5 0 0 1-3.5 3.5h-7A3.5 3.5 0 0 1 5 16.5v-9Z" />
      <path d="M9 8h6" />
      <path d="M9 12h6" />
      <path d="M9 16h3" />
    </svg>
  );
}

function LockIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-3 w-3 shrink-0 text-muted-light"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M6.5 9V6.5a3.5 3.5 0 1 1 7 0V9" />
      <rect x="4.75" y="9" width="10.5" height="7.25" rx="1.5" />
    </svg>
  );
}

function ChevronUpIcon({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 20 20"
      className={[
        "h-3 w-3 shrink-0 text-muted-light transition-transform",
        open ? "rotate-180" : "",
      ].join(" ")}
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M5 12.5 10 7.5 15 12.5" />
    </svg>
  );
}
