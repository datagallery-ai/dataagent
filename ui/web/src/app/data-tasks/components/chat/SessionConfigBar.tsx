"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import {
  PER_RUN_MENTION_APPEARANCE,
  isConfigItemUsable,
  isSessionResourceKindLocked,
  sessionResourceCounts,
} from "../../data-task-state";
import type {
  ChatSession,
  ConfigItemStatus,
  PerRunMentionKind,
  SessionStartedHints,
  WorkspaceConfigItem,
  WorkspaceConfigStore,
} from "../../data-task-state";
import { ResourceKindIcon } from "./SessionResourceSummary";
import { useT } from "../../../../i18n/locale-context";
import {
  translateConfigItemStatus,
  translateSessionResourceLabel,
} from "../../../../i18n/status-labels";

const SESSION_CONFIG_PORTAL_ATTR = "data-session-config-portal";
const SESSION_CONFIG_PILLS = ["db", "kb", "agent-tools"] as const;
type SessionConfigPillKey = PerRunMentionKind | "agent-tools";

type SessionConfigBarProps = {
  workspaceConfig: WorkspaceConfigStore;
  session: ChatSession | null;
  sessionStartedHints?: SessionStartedHints;
  onToggleSessionResource: (kind: PerRunMentionKind, id: string) => void;
  leading?: ReactNode;
  trailing?: ReactNode;
};

/** Below this bar width, pills keep icons only (hide count annotations). */
const SESSION_CONFIG_COMPACT_WIDTH = 420;

export function SessionConfigBar({
  workspaceConfig,
  session,
  sessionStartedHints,
  onToggleSessionResource,
  leading,
  trailing,
}: SessionConfigBarProps) {
  const [openPanel, setOpenPanel] = useState<SessionConfigPillKey | null>(null);
  const [compact, setCompact] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const isInsideSessionConfigUi = useCallback((target: Node) => {
    if (rootRef.current?.contains(target)) return true;
    return (target as HTMLElement).closest?.(`[${SESSION_CONFIG_PORTAL_ATTR}]`) != null;
  }, []);

  useLayoutEffect(() => {
    const node = rootRef.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const update = (width: number) => {
      const nextCompact = width < SESSION_CONFIG_COMPACT_WIDTH;
      setCompact((current) => (current === nextCompact ? current : nextCompact));
    };
    update(node.getBoundingClientRect().width);
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (typeof width === "number") update(width);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!openPanel) return;
    const handlePointerDown = (event: globalThis.MouseEvent) => {
      if (!isInsideSessionConfigUi(event.target as Node)) {
        setOpenPanel(null);
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [isInsideSessionConfigUi, openPanel]);

  const togglePanel = useCallback((key: SessionConfigPillKey) => {
    setOpenPanel((current) => (current === key ? null : key));
  }, []);

  const renderPill = (key: (typeof SESSION_CONFIG_PILLS)[number]) => {
    if (key === "agent-tools") {
      return (
        <AgentToolsPill
          key={key}
          workspaceConfig={workspaceConfig}
          session={session}
          open={openPanel === key}
          compact={compact}
          onToggleOpen={() => togglePanel(key)}
          onToggleResource={onToggleSessionResource}
        />
      );
    }

    return (
      <SessionConfigPill
        key={key}
        kind={key}
        items={workspaceConfig[key]}
        counts={sessionResourceCounts(workspaceConfig, key, session)}
        open={openPanel === key}
        compact={compact}
        session={session}
        sessionStartedHints={sessionStartedHints}
        onToggleOpen={() => togglePanel(key)}
        onToggleResource={(id) => onToggleSessionResource(key, id)}
      />
    );
  };

  return (
    <div
      ref={rootRef}
      className="relative flex w-full min-w-0 items-center gap-2 px-3 pb-1.5 pt-1"
      data-testid="session-config-bar"
      data-compact={compact ? "true" : "false"}
    >
      {leading ? (
        <div className="flex shrink-0 items-center self-center">{leading}</div>
      ) : null}
      <div className="flex min-w-0 flex-1 flex-nowrap items-center gap-1 overflow-hidden">
        {SESSION_CONFIG_PILLS.map((key) => renderPill(key))}
      </div>
      {trailing ? (
        <div className="flex min-w-0 shrink items-center justify-end gap-1">{trailing}</div>
      ) : null}
    </div>
  );
}

function preventFocusSteal(event: MouseEvent) {
  const target = event.target as HTMLElement;
  if (target.closest('button, input, [role="switch"]')) return;
  event.preventDefault();
}

function AnchoredPortal({
  anchorRef,
  open,
  children,
  minWidth = 220,
}: {
  anchorRef: RefObject<HTMLElement | null>;
  open: boolean;
  children: ReactNode;
  minWidth?: number;
}) {
  const [coords, setCoords] = useState<{
    left: number;
    bottom: number;
    width: number;
  } | null>(null);

  useLayoutEffect(() => {
    if (!open || !anchorRef.current) {
      setCoords(null);
      return;
    }

    const update = () => {
      const anchor = anchorRef.current;
      if (!anchor) return;
      const rect = anchor.getBoundingClientRect();
      const width = Math.min(Math.max(minWidth, rect.width), window.innerWidth - 32);
      const left = Math.min(
        Math.max(16, rect.left),
        window.innerWidth - width - 16,
      );
      setCoords({
        left,
        bottom: window.innerHeight - rect.top + 8,
        width,
      });
    };

    update();
    const observer = new ResizeObserver(update);
    observer.observe(anchorRef.current);
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [anchorRef, minWidth, open]);

  if (!open || !coords || typeof document === "undefined") return null;

  return createPortal(
    <div
      {...{ [SESSION_CONFIG_PORTAL_ATTR]: "" }}
      className="pointer-events-auto"
      style={{
        position: "fixed",
        left: coords.left,
        bottom: coords.bottom,
        width: coords.width,
        zIndex: 200,
      }}
    >
      {children}
    </div>,
    document.body,
  );
}

function SessionConfigPill({
  kind,
  items,
  counts,
  open,
  compact = false,
  session,
  sessionStartedHints,
  onToggleOpen,
  onToggleResource,
  rootRef,
}: {
  kind: PerRunMentionKind;
  items: WorkspaceConfigItem[];
  counts: { enabled: number; total: number };
  open: boolean;
  compact?: boolean;
  session: ChatSession | null;
  sessionStartedHints?: SessionStartedHints;
  onToggleOpen: () => void;
  onToggleResource: (id: string) => void;
  rootRef?: (node: HTMLDivElement | null) => void;
}) {
  const t = useT();
  const anchorRef = useRef<HTMLDivElement>(null);
  const appearance = PER_RUN_MENTION_APPEARANCE[kind];
  const label = translateSessionResourceLabel(kind, t);
  const locked = isSessionResourceKindLocked(session, kind, sessionStartedHints);
  const countLabel = `${counts.enabled}/${counts.total}`;

  const setRefs = useCallback(
    (node: HTMLDivElement | null) => {
      anchorRef.current = node;
      rootRef?.(node);
    },
    [rootRef],
  );

  return (
    <div ref={setRefs} className="relative shrink-0">
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${label} session settings, ${countLabel}`}
        title={`${label}: ${countLabel}`}
        onClick={onToggleOpen}
        className={[
          "inline-flex items-center rounded-full border text-xs font-medium transition",
          compact ? "gap-1 px-1.5 py-1" : "gap-1.5 px-2 py-1",
          open ? appearance.pillOpen : appearance.pill,
        ].join(" ")}
      >
        {compact ? null : <ChevronUpIcon open={open} />}
        <span
          className={[
            "inline-flex h-4 w-4 items-center justify-center rounded-md",
            appearance.badge,
          ].join(" ")}
          aria-hidden
        >
          <ResourceKindIcon kind={kind} className="h-3 w-3" />
        </span>
        {compact ? null : <span className="opacity-70">{countLabel}</span>}
        {locked ? <LockIcon /> : null}
      </button>

      <AnchoredPortal anchorRef={anchorRef} open={open}>
        <SessionConfigPillPanel
          kind={kind}
          items={items}
          session={session}
          locked={locked}
          onToggleResource={onToggleResource}
        />
      </AnchoredPortal>
    </div>
  );
}

function AgentToolsPill({
  workspaceConfig,
  session,
  open,
  compact = false,
  onToggleOpen,
  onToggleResource,
}: {
  workspaceConfig: WorkspaceConfigStore;
  session: ChatSession | null;
  open: boolean;
  compact?: boolean;
  onToggleOpen: () => void;
  onToggleResource: (kind: PerRunMentionKind, id: string) => void;
}) {
  const t = useT();
  const anchorRef = useRef<HTMLDivElement>(null);
  const mcpCounts = sessionResourceCounts(workspaceConfig, "mcp", session);
  const skillCounts = sessionResourceCounts(workspaceConfig, "skill", session);
  const enabled = mcpCounts.enabled + skillCounts.enabled;
  const total = mcpCounts.total + skillCounts.total;
  const countLabel = `${enabled}/${total}`;
  const appearance = PER_RUN_MENTION_APPEARANCE.mcp;
  const agentToolsLabel = t("resources.agentTools");

  return (
    <div ref={anchorRef} className="relative shrink-0">
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${t("sessionConfig.agentToolsSettings")}, ${countLabel}`}
        title={`${agentToolsLabel}: ${countLabel}`}
        onClick={onToggleOpen}
        className={[
          "inline-flex items-center rounded-full border text-xs font-medium transition",
          compact ? "gap-1 px-1.5 py-1" : "gap-1.5 px-2 py-1",
          open ? appearance.pillOpen : appearance.pill,
        ].join(" ")}
      >
        {compact ? null : <ChevronUpIcon open={open} />}
        <span
          className={[
            "inline-flex h-4 w-4 items-center justify-center rounded-md",
            appearance.badge,
          ].join(" ")}
          aria-hidden
        >
          <AgentToolsIcon />
        </span>
        {compact ? null : <span className="opacity-70">{countLabel}</span>}
      </button>

      <AnchoredPortal anchorRef={anchorRef} open={open} minWidth={280}>
        <AgentToolsPillPanel
          workspaceConfig={workspaceConfig}
          session={session}
          onToggleResource={onToggleResource}
        />
      </AnchoredPortal>
    </div>
  );
}

function AgentToolsPillPanel({
  workspaceConfig,
  session,
  onToggleResource,
}: {
  workspaceConfig: WorkspaceConfigStore;
  session: ChatSession | null;
  onToggleResource: (kind: PerRunMentionKind, id: string) => void;
}) {
  const t = useT();
  return (
    <div
      role="listbox"
      aria-label={t("sessionConfig.agentToolsSettings")}
      className="overflow-hidden rounded-2xl border border-border bg-surface shadow-xl"
      onMouseDown={preventFocusSteal}
    >
      <div className="border-b border-border px-3 py-2.5">
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs font-semibold text-foreground">{t("resources.agentTools")}</span>
          <span className="rounded-full border border-border bg-surface-subtle px-2 py-0.5 text-[10px] font-medium text-muted-light">
            {t("sessionConfig.currentChat")}
          </span>
        </div>
        <p className="mt-1 text-[11px] leading-4 text-muted-light">
          {t("sessionConfig.agentToolsScopeHelp")}
        </p>
      </div>
      <AgentToolsSection
        kind="mcp"
        items={workspaceConfig.mcp}
        session={session}
        onToggleResource={onToggleResource}
      />
      <AgentToolsSection
        kind="skill"
        items={workspaceConfig.skill}
        session={session}
        onToggleResource={onToggleResource}
      />
    </div>
  );
}

function AgentToolsSection({
  kind,
  items,
  session,
  onToggleResource,
}: {
  kind: PerRunMentionKind;
  items: WorkspaceConfigItem[];
  session: ChatSession | null;
  onToggleResource: (kind: PerRunMentionKind, id: string) => void;
}) {
  const t = useT();
  const label = translateSessionResourceLabel(kind, t);

  return (
    <section className="border-b border-border last:border-b-0">
      <div className="flex items-center gap-1.5 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.06em] text-muted-light">
        <ResourceKindIcon kind={kind} className="h-3.5 w-3.5" />
        {label}
      </div>
      {items.length === 0 ? (
        <p className="px-3 pb-3 text-sm text-muted-light">
          {t("sessionConfig.noConfigurationShort")}
        </p>
      ) : (
        <ul className="max-h-40 overflow-y-auto py-1">
          {items.map((item) => {
            const enabled = !new Set(session?.config?.disabled[kind] ?? []).has(
              item.id,
            );
            const usable = isConfigItemUsable(item);
            return (
              <li key={item.id}>
                <div
                  className={[
                    "flex items-start gap-3 px-3 py-2 transition",
                    usable ? "hover:bg-surface-subtle" : "opacity-60",
                  ].join(" ")}
                >
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="min-w-0 truncate text-sm font-medium text-foreground">
                        {item.name}
                      </span>
                      {usable ? null : <ConfigStatusChip status={item.status} />}
                    </span>
                    {item.description && (
                      <span className="mt-0.5 block truncate text-xs text-muted-light">
                        {item.description}
                      </span>
                    )}
                  </span>
                  <Switch
                    checked={enabled && usable}
                    disabled={!usable}
                    onChange={() => onToggleResource(kind, item.id)}
                    aria-label={`${enabled ? t("common.disable") : t("common.enable")} ${item.name}`}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function SessionConfigPillPanel({
  kind,
  items,
  session,
  locked,
  onToggleResource,
}: {
  kind: PerRunMentionKind;
  items: WorkspaceConfigItem[];
  session: ChatSession | null;
  locked: boolean;
  onToggleResource: (id: string) => void;
}) {
  const t = useT();
  const label = translateSessionResourceLabel(kind, t);

  return (
    <div
      role="listbox"
      aria-label={`${label} session settings`}
      className="overflow-hidden rounded-2xl border border-border bg-surface shadow-xl"
      onMouseDown={preventFocusSteal}
    >
      <div className="border-b border-border px-3 py-2.5">
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs font-semibold text-foreground">{label}</span>
          <span className="rounded-full border border-border bg-surface-subtle px-2 py-0.5 text-[10px] font-medium text-muted-light">
            {locked ? t("sessionConfig.locked") : t("sessionConfig.currentChat")}
          </span>
        </div>
        <p className="mt-1 text-[11px] leading-4 text-muted-light">
          {locked ? t("sessionConfig.lockedHelp") : t("sessionConfig.scopeHelp")}
        </p>
      </div>
      {items.length === 0 ? (
        <p className="px-3 py-4 text-sm text-muted-light">
          {t("sessionConfig.noConfigurationYet")}
        </p>
      ) : (
        <ul className="max-h-56 overflow-y-auto py-1">
          {items.map((item) => {
            const enabled = !new Set(session?.config?.disabled[kind] ?? []).has(
              item.id,
            );
            const usable = isConfigItemUsable(item);
            return (
              <li key={item.id}>
                <div
                  className={[
                    "flex items-start gap-3 px-3 py-2 transition",
                    usable ? "hover:bg-surface-subtle" : "opacity-60",
                  ].join(" ")}
                >
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="min-w-0 truncate text-sm font-medium text-foreground">
                        {item.name}
                      </span>
                      {usable ? null : <ConfigStatusChip status={item.status} />}
                    </span>
                    {item.description && (
                      <span className="mt-0.5 block truncate text-xs text-muted-light">
                        {item.description}
                      </span>
                    )}
                  </span>
                  <Switch
                    checked={enabled && usable}
                    disabled={locked || !usable}
                    onChange={() => onToggleResource(item.id)}
                    aria-label={`${enabled ? t("common.disable") : t("common.enable")} ${item.name}`}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function Switch({
  checked,
  disabled = false,
  onChange,
  "aria-label": ariaLabel,
}: {
  checked: boolean;
  disabled?: boolean;
  onChange: () => void;
  "aria-label": string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-disabled={disabled}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={(event) => {
        event.stopPropagation();
        if (disabled) return;
        onChange();
      }}
      className={[
        "relative z-10 h-5 w-9 shrink-0 self-center rounded-full transition",
        checked ? "bg-primary" : "bg-border",
        disabled ? "cursor-not-allowed opacity-50" : "",
      ].join(" ")}
    >
      <span
        className={[
          "pointer-events-none absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-surface shadow transition-transform",
          checked ? "translate-x-4" : "translate-x-0",
        ].join(" ")}
      />
    </button>
  );
}

function ConfigStatusChip({ status }: { status: ConfigItemStatus | undefined }) {
  const t = useT();
  return (
    <span
      className={[
        "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium",
        status === "failed"
          ? "bg-rose-50 text-rose-600"
          : "bg-slate-100 text-slate-500",
      ].join(" ")}
    >
      {translateConfigItemStatus(status, t)}
    </span>
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

function AgentToolsIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-3 w-3"
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
      <path d="M4 4h5v5H4z" />
      <path d="M16 16h4v4h-4z" />
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
