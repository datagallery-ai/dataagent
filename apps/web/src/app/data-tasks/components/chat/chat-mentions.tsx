"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import {
  PER_RUN_MENTION_APPEARANCE,
  PER_RUN_MENTION_KINDS,
  PER_RUN_MENTION_META,
} from "../../data-task-state";
import type {
  FileMentionResource,
  MentionResource,
  PerRunMentionKind,
  PerRunFileSelection,
  PerRunSelection,
} from "../../data-task-state";
import { scheduleChatTextareaResize } from "./use-chat-textarea-autoresize";

// Matches an in-progress `@token` immediately before the caret. Allowed query
// chars: letters/digits/_/- (so a trailing space or punctuation closes it).
const MENTION_TOKEN_RE = /(?:^|\s)@([\p{L}\p{N}_-]*)$/u;

/**
 * CopilotKit handles Enter on the textarea via React onKeyDown. While the `@`
 * menu is open we register a document capture listener so ↑/↓/Enter/Esc win
 * reliably (including under React 19's per-node delegation).
 */
function setNativeTextareaValue(
  el: HTMLTextAreaElement,
  value: string,
  caret: number,
): void {
  const descriptor = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  );
  descriptor?.set?.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  requestAnimationFrame(() => {
    el.focus({ preventScroll: true });
    el.setSelectionRange(caret, caret);
    scheduleChatTextareaResize();
  });
}

type MentionMenuState = { query: string; tokenStart: number; caret: number };
type MentionMenuItem =
  | { type: "resource"; resource: MentionResource }
  | { type: "file"; resource: FileMentionResource };

export interface MentionAutocomplete {
  /** Attach to the element that wraps the CopilotChat textarea. */
  columnRef: (node: HTMLDivElement | null) => void;
  /** The floating picker; render it inside the (relative) input card. */
  menu: ReactNode;
  /** Insert an `@` at the caret and open the picker (toolbar-button entry point). */
  openAtCaret: () => void;
}

export function useMentionAutocomplete({
  resources,
  fileResources,
  selection,
  fileSelection,
  onToggle,
  onToggleFile,
  refreshToken,
}: {
  resources: MentionResource[];
  fileResources?: FileMentionResource[];
  selection: PerRunSelection;
  fileSelection?: PerRunFileSelection;
  onToggle: (kind: PerRunMentionKind, id: string) => void;
  onToggleFile?: (resource: FileMentionResource) => void;
  /** Change this (e.g. the input `mode`) to re-bind after the textarea remounts. */
  refreshToken?: string;
}): MentionAutocomplete {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const columnNodeRef = useRef<HTMLDivElement | null>(null);
  const [menuState, setMenuState] = useState<MentionMenuState | null>(null);
  const [highlight, setHighlight] = useState(0);

  const filtered = useMemo(() => {
    if (!menuState) return [];
    const query = menuState.query.trim().toLowerCase();
    const items: MentionMenuItem[] = [
      ...resources.map((resource) => ({ type: "resource" as const, resource })),
      ...(fileResources ?? []).map((resource) => ({
        type: "file" as const,
        resource,
      })),
    ];
    if (query.length === 0) return items;
    return items.filter((item) => {
      if (item.type === "resource") {
        const resource = item.resource;
        const meta = PER_RUN_MENTION_META[resource.kind];
        const haystack = [
          resource.name,
          resource.description,
          meta.token,
          meta.label,
        ];
        return haystack.join(" ").toLowerCase().includes(query);
      }
      const resource = item.resource;
      const haystack = [
        resource.name,
        resource.description,
        "file",
        resource.scope === "workspace" ? "Workspace" : "This chat",
      ];
      return haystack.join(" ").toLowerCase().includes(query);
    });
  }, [fileResources, menuState, resources]);

  useEffect(() => {
    setHighlight(0);
  }, [menuState?.query]);

  const closeMenu = useCallback(() => setMenuState(null), []);

  const syncFromCaret = useCallback(() => {
    const el = textareaRef.current;
    if (!el) {
      setMenuState(null);
      return;
    }
    const caret = el.selectionStart ?? el.value.length;
    const before = el.value.slice(0, caret);
    const match = before.match(MENTION_TOKEN_RE);
    if (!match) {
      setMenuState(null);
      return;
    }
    const query = match[1] ?? "";
    setMenuState({ query, tokenStart: caret - query.length - 1, caret });
  }, []);

  const selectResource = useCallback(
    (item: MentionMenuItem) => {
      const el = textareaRef.current;
      const state = menuState;
      if (el && state) {
        const next =
          el.value.slice(0, state.tokenStart) + el.value.slice(state.caret);
        setNativeTextareaValue(el, next, state.tokenStart);
      }
      if (item.type === "resource") {
        onToggle(item.resource.kind, item.resource.id);
      } else {
        onToggleFile?.(item.resource);
      }
      setMenuState(null);
    },
    [menuState, onToggle, onToggleFile],
  );

  const filteredRef = useRef(filtered);
  filteredRef.current = filtered;
  const highlightRef = useRef(highlight);
  highlightRef.current = highlight;
  const selectResourceRef = useRef(selectResource);
  selectResourceRef.current = selectResource;

  const handleBlurRef = useRef(() => {
    // Delay so a click on a menu item registers before the menu unmounts.
    window.setTimeout(() => setMenuState(null), 120);
  });

  const bindTextarea = useCallback(
    (node: HTMLDivElement | null) => {
      const previous = textareaRef.current;
      const nextTextarea = node?.querySelector("textarea") ?? null;
      if (previous === nextTextarea) return;
      if (previous) {
        previous.removeEventListener("input", syncFromCaret);
        previous.removeEventListener("keyup", syncFromCaret);
        previous.removeEventListener("click", syncFromCaret);
        previous.removeEventListener("blur", handleBlurRef.current);
      }
      textareaRef.current = nextTextarea;
      if (nextTextarea) {
        nextTextarea.addEventListener("input", syncFromCaret);
        nextTextarea.addEventListener("keyup", syncFromCaret);
        nextTextarea.addEventListener("click", syncFromCaret);
        nextTextarea.addEventListener("blur", handleBlurRef.current);
      }
    },
    [syncFromCaret],
  );

  const columnRef = useCallback(
    (node: HTMLDivElement | null) => {
      columnNodeRef.current = node;
      bindTextarea(node);
    },
    [bindTextarea],
  );

  // The textarea unmounts in transcribe/processing mode; re-bind when it returns.
  useEffect(() => {
    bindTextarea(columnNodeRef.current);
  }, [bindTextarea, refreshToken]);

  // React 19 attaches onKeyDown on the textarea; document capture runs first and
  // reliably intercepts ↑/↓/Enter/Esc for the @ menu while it is open.
  useEffect(() => {
    if (menuState === null) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      const textarea = textareaRef.current;
      if (!textarea || event.target !== textarea) return;
      if (event.isComposing || event.keyCode === 229) return;

      const items = filteredRef.current;
      switch (event.key) {
        case "ArrowDown":
          event.preventDefault();
          event.stopImmediatePropagation();
          setHighlight((index) =>
            items.length === 0 ? 0 : (index + 1) % items.length,
          );
          break;
        case "ArrowUp":
          event.preventDefault();
          event.stopImmediatePropagation();
          setHighlight((index) =>
            items.length === 0 ? 0 : (index - 1 + items.length) % items.length,
          );
          break;
        case "Enter":
        case "Tab": {
          const choice = items[highlightRef.current];
          if (choice) {
            event.preventDefault();
            event.stopImmediatePropagation();
            selectResourceRef.current(choice);
          }
          break;
        }
        case "Escape":
          event.preventDefault();
          event.stopImmediatePropagation();
          setMenuState(null);
          break;
        default:
          break;
      }
    };

    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  }, [menuState !== null]);

  const openAtCaret = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.focus({ preventScroll: true });
    const caret = el.selectionStart ?? el.value.length;
    const before = el.value.slice(0, caret);
    const insert = before.length > 0 && !/\s$/.test(before) ? " @" : "@";
    const next = el.value.slice(0, caret) + insert + el.value.slice(caret);
    setNativeTextareaValue(el, next, caret + insert.length);
    requestAnimationFrame(() => syncFromCaret());
  }, [syncFromCaret]);

  const menu =
    menuState !== null ? (
      <MentionMenu
        items={filtered}
        highlight={highlight}
        selection={selection}
        fileSelection={fileSelection}
        onHover={setHighlight}
        onPick={selectResource}
        onClose={closeMenu}
      />
    ) : null;

  return { columnRef, menu, openAtCaret };
}

function MentionMenu({
  items,
  highlight,
  selection,
  fileSelection,
  onHover,
  onPick,
  onClose,
}: {
  items: MentionMenuItem[];
  highlight: number;
  selection: PerRunSelection;
  fileSelection?: PerRunFileSelection;
  onHover: (index: number) => void;
  onPick: (item: MentionMenuItem) => void;
  onClose: () => void;
}) {
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => {
    optionRefs.current[highlight]?.scrollIntoView({ block: "nearest" });
  }, [highlight, items.length]);

  return (
    <div
      role="listbox"
      aria-label="Select capabilities (@)"
      className="absolute bottom-full left-0 z-50 mb-2 w-[min(420px,calc(100vw-2rem))] overflow-hidden rounded-xl border border-border bg-surface shadow-lg"
      onMouseDown={(event) => event.preventDefault()}
    >
      <div className="flex items-center justify-between gap-3 border-b border-border px-3 py-2">
        <span className="text-[11px] font-medium uppercase tracking-[0.06em] text-muted-light">
          Select run capabilities with @
        </span>
        <div className="flex shrink-0 items-center gap-1.5 text-[11px] text-muted-light">
          <span className="rounded bg-surface-subtle px-1.5 py-0.5 font-medium text-muted">
            ↑↓ Move
          </span>
          <span className="hidden sm:inline">Enter Select</span>
          <button
            type="button"
            onClick={onClose}
            className="rounded px-1.5 py-0.5 transition hover:bg-surface-subtle hover:text-muted"
          >
            Esc
          </button>
        </div>
      </div>
      {items.length === 0 ? (
        <p className="px-3 py-4 text-sm text-muted-light">
          No matching resources. Add one from the configuration panel first.
        </p>
      ) : (
        <ul className="max-h-72 overflow-y-auto py-1">
          {items.map((item, index) => {
            const resource = item.resource;
            const active = index === highlight;
            const selected =
              item.type === "resource"
                ? selection[item.resource.kind].includes(item.resource.id)
                : isFileMentionSelected(item.resource, fileSelection);
            const label =
              item.type === "resource"
                ? `@${PER_RUN_MENTION_META[item.resource.kind].token}`
                : "@file";
            const badgeClass =
              item.type === "resource"
                ? PER_RUN_MENTION_APPEARANCE[item.resource.kind].badge
                : "bg-indigo-50 text-indigo-700";
            return (
              <li key={`${item.type}:${resource.id}`}>
                <button
                  type="button"
                  role="option"
                  aria-selected={active}
                  ref={(node) => {
                    optionRefs.current[index] = node;
                  }}
                  onMouseEnter={() => onHover(index)}
                  onClick={() => onPick(item)}
                  className={[
                    "flex w-full items-center gap-2 px-3 py-2 text-left transition",
                    active ? "bg-slate-100" : "",
                  ].join(" ")}
                >
                  <span
                    className={[
                      "inline-flex shrink-0 items-center rounded-md px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                      badgeClass,
                    ].join(" ")}
                  >
                    {label}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="truncate text-sm font-medium text-foreground">
                        {resource.name}
                      </span>
                    </span>
                    {resource.description && (
                      <span className="mt-0.5 block truncate text-xs text-muted-light">
                        {item.type === "file"
                          ? `${item.resource.scope === "workspace" ? "Workspace" : "This chat"} · ${resource.description}`
                          : resource.description}
                      </span>
                    )}
                  </span>
                  {selected && (
                    <span className="shrink-0 self-center text-muted">
                      <CheckIcon />
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function MentionChips({
  resources,
  fileResources,
  selection,
  fileSelection,
  onRemove,
  onRemoveFile,
  onClear,
  onClearFiles,
}: {
  resources: MentionResource[];
  fileResources?: FileMentionResource[];
  selection: PerRunSelection;
  fileSelection?: PerRunFileSelection;
  onRemove: (kind: PerRunMentionKind, id: string) => void;
  onRemoveFile?: (resource: FileMentionResource) => void;
  onClear: () => void;
  onClearFiles?: () => void;
}) {
  const byId = useMemo(() => {
    const map = new Map<string, MentionResource>();
    for (const resource of resources) {
      map.set(`${resource.kind}:${resource.id}`, resource);
    }
    return map;
  }, [resources]);

  const chips: MentionResource[] = [];
  for (const kind of PER_RUN_MENTION_KINDS) {
    for (const id of selection[kind]) {
      const resource = byId.get(`${kind}:${id}`);
      if (resource) chips.push(resource);
    }
  }
  const fileChips = (fileResources ?? []).filter((resource) =>
    isFileMentionSelected(resource, fileSelection),
  );
  if (chips.length === 0 && fileChips.length === 0) return null;

  return (
    <div
      data-mention-chips
      className="flex w-full cursor-default select-none flex-wrap items-center gap-1.5"
    >
      {chips.map((resource) => {
        const meta = PER_RUN_MENTION_META[resource.kind];
        const appearance = PER_RUN_MENTION_APPEARANCE[resource.kind];
        return (
          <span
            key={`${resource.kind}:${resource.id}`}
            className={[
              "inline-flex items-center gap-1 rounded-full border py-0.5 pl-2 pr-1 text-xs",
              appearance.chip,
            ].join(" ")}
            title={`${meta.label}: ${resource.name}`}
          >
            <span
              className={[
                "rounded px-1 py-px text-[10px] font-semibold uppercase tracking-wide",
                appearance.badge,
              ].join(" ")}
            >
              @{meta.token}
            </span>
            <span className="max-w-[140px] truncate font-medium">
              {resource.name}
            </span>
            <button
              type="button"
              aria-label={`Remove ${resource.name}`}
              onClick={() => onRemove(resource.kind, resource.id)}
              className="ml-0.5 grid h-4 w-4 place-items-center rounded-full text-muted-light transition hover:bg-surface-subtle hover:text-muted"
            >
              <CloseIcon />
            </button>
          </span>
        );
      })}
      {fileChips.map((resource) => (
        <span
          key={`file:${resource.id}`}
          className="inline-flex items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 py-0.5 pl-2 pr-1 text-xs text-indigo-700"
          title={`File: ${resource.name}`}
        >
          <span className="rounded bg-white/70 px-1 py-px text-[10px] font-semibold uppercase tracking-wide">
            @file
          </span>
          <span className="max-w-[140px] truncate font-medium">
            {resource.name}
          </span>
          {!resource.backendSupported ? (
            <span className="rounded bg-white/70 px-1 py-px text-[10px]">
              Backend unsupported
            </span>
          ) : null}
          <button
            type="button"
            aria-label={`Remove ${resource.name}`}
            onClick={() => onRemoveFile?.(resource)}
            className="ml-0.5 grid h-4 w-4 place-items-center rounded-full text-indigo-400 transition hover:bg-white/70 hover:text-indigo-700"
          >
            <CloseIcon />
          </button>
        </span>
      ))}
      <button
        type="button"
        onClick={() => {
          onClear();
          onClearFiles?.();
        }}
        className="ml-0.5 rounded-full px-2 py-0.5 text-[11px] text-muted-light transition hover:bg-surface-subtle hover:text-muted"
      >
        Clear
      </button>
    </div>
  );
}

function isFileMentionSelected(
  resource: FileMentionResource,
  selection?: PerRunFileSelection,
): boolean {
  if (!selection) return false;
  return resource.scope === "workspace"
    ? selection.fileIds.includes(resource.fileId)
    : Boolean(resource.path && selection.pinnedPaths.includes(resource.path));
}

function CheckIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="m5 10 3 3 7-7" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-3 w-3"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M6 6l8 8M14 6l-8 8" />
    </svg>
  );
}
