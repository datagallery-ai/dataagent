import type { DataArtifactType, DataStepKind } from "./data-task-state";

/** Shared Tailwind class bundles for the data-tasks workbench. */

export const panelShellClass =
  "min-w-0 max-w-full rounded-xl border border-border bg-surface p-3 shadow-[var(--shadow-card)]";

export const panelTitleClass = "text-sm font-semibold text-foreground";

export const sectionLabelClass =
  "text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-light";

export const metricValueClass = "tabular text-lg font-semibold text-foreground";

export const kpiValueClass = "tabular text-2xl font-semibold tracking-tight text-foreground";

export const metricLabelClass = "text-[11px] font-medium text-muted-light";

/** Secondary actions — pair with `.hitl-action-btn-secondary` in globals.css inside CopilotKit. */
export const btnSecondaryClass =
  "cursor-pointer rounded-lg border border-border bg-surface px-3 py-1.5 text-xs font-medium text-muted transition-colors duration-200 hover:border-muted-light hover:bg-surface-subtle hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20";

/** ask_user / HITL selectable option rows — pair with `.hitl-choice-option` in globals.css. */
export const choiceOptionClass =
  "flex w-full cursor-pointer items-center gap-3 rounded-lg px-3 py-2.5 text-left transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20";

export const choiceOptionIconClass =
  "flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border bg-surface text-[11px] font-semibold text-muted-light transition-colors duration-200 group-hover:border-muted-light group-hover:bg-surface-subtle group-hover:text-foreground";

export const choiceOptionChevronClass =
  "shrink-0 text-muted-light transition-colors duration-200 group-hover:text-foreground";

export const btnPrimaryClass =
  "cursor-pointer rounded-lg bg-primary px-3 py-1.5 text-xs font-semibold text-white transition-colors duration-200 hover:bg-primary-light focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/25";

export const btnGhostClass =
  "cursor-pointer rounded-lg px-2.5 py-1 text-xs font-medium text-muted transition-colors duration-200 hover:bg-surface-subtle hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20";

export const chipClass =
  "inline-flex items-center gap-1.5 rounded-full border border-border bg-surface-subtle px-2.5 py-0.5 text-[11px] font-medium text-muted";

export const codeBlockClass =
  "max-w-full overflow-x-auto overflow-y-auto rounded-xl bg-code-bg p-3 font-mono text-[11px] leading-5 text-slate-100";

export const dataTableShellClass =
  "max-w-full overflow-x-auto overscroll-x-contain rounded-xl border border-border";

export const dataTableClass = "w-full min-w-max text-left text-[11px]";

export const dataTableHeadClass =
  "sticky top-0 z-10 bg-surface-subtle text-muted-light shadow-[0_1px_0_0_var(--border)]";

export const dataTableRowClass =
  "border-t border-border transition-colors duration-150 hover:bg-primary-light/5";

export const dataTableCellClass = "whitespace-nowrap px-2.5 py-1.5 text-muted";

export const dataTableCellNumericClass =
  "whitespace-nowrap px-2.5 py-1.5 text-right tabular text-muted";

export const emptyStateClass = "rounded-lg border border-dashed border-border bg-surface-subtle p-4 text-center";

export type ToneClassBundle = {
  bg: string;
  border: string;
  text: string;
  bar: string;
  ring: string;
};

const stepKindTones = {
  inspect: {
    bg: "bg-step-inspect/10",
    border: "border-step-inspect/30",
    text: "text-step-inspect",
    bar: "bg-step-inspect",
    ring: "ring-step-inspect/20",
  },
  query: {
    bg: "bg-step-query/10",
    border: "border-step-query/30",
    text: "text-step-query",
    bar: "bg-step-query",
    ring: "ring-step-query/20",
  },
  transform: {
    bg: "bg-step-transform/10",
    border: "border-step-transform/30",
    text: "text-step-transform",
    bar: "bg-step-transform",
    ring: "ring-step-transform/20",
  },
  fetch: {
    bg: "bg-step-fetch/10",
    border: "border-step-fetch/30",
    text: "text-step-fetch",
    bar: "bg-step-fetch",
    ring: "ring-step-fetch/20",
  },
  visualize: {
    bg: "bg-step-visualize/10",
    border: "border-step-visualize/30",
    text: "text-step-visualize",
    bar: "bg-step-visualize",
    ring: "ring-step-visualize/20",
  },
  knowledge: {
    bg: "bg-step-knowledge/10",
    border: "border-step-knowledge/30",
    text: "text-step-knowledge",
    bar: "bg-step-knowledge",
    ring: "ring-step-knowledge/20",
  },
  other: {
    bg: "bg-surface-subtle",
    border: "border-border",
    text: "text-muted",
    bar: "bg-muted-light",
    ring: "ring-border",
  },
} satisfies Record<DataStepKind, ToneClassBundle>;

export function stepKindTone(kind: DataStepKind): ToneClassBundle {
  return stepKindTones[kind];
}

export type ArtifactToneBundle = ToneClassBundle & {
  icon: string;
  label: string;
};

const artifactTones = {
  dataset: {
    ...stepKindTones.query,
    icon: "▦",
    label: "dataset",
  },
  sql: {
    ...stepKindTones.inspect,
    icon: "SQL",
    label: "sql",
  },
  chart: {
    ...stepKindTones.visualize,
    icon: "◇",
    label: "chart",
  },
  report: {
    ...stepKindTones.knowledge,
    icon: "¶",
    label: "report",
  },
  file: {
    ...stepKindTones.transform,
    icon: "□",
    label: "file",
  },
} satisfies Record<DataArtifactType, ArtifactToneBundle>;

export function artifactToneForType(type?: DataArtifactType | string): ArtifactToneBundle {
  if (type && type in artifactTones) {
    return artifactTones[type as DataArtifactType];
  }
  return {
    ...stepKindTones.other,
    icon: "•",
    label: type ?? "artifact",
  };
}

export type StatusToneKind = "success" | "error" | "info" | "warning" | "muted";

const statusTones: Record<StatusToneKind, string> = {
  success: "border-step-success/30 bg-step-success/10 text-step-success",
  error: "border-step-error/30 bg-step-error/10 text-step-error",
  info: "border-primary-light/30 bg-primary-light/10 text-primary",
  warning: "border-step-warning/30 bg-step-warning/10 text-step-warning",
  muted: "border-border bg-surface-subtle text-muted",
};

/** Semantic status banner / pill surface classes. */
export function statusTone(kind: StatusToneKind): string {
  return statusTones[kind];
}

/** Full-screen overlay backdrop for drawers and trace modals. */
export const overlayBackdropClass =
  "fixed inset-0 z-50 bg-foreground/40 backdrop-blur-sm";

/** Centered overlay panel shell. */
export const overlayPanelClass =
  "flex flex-col overflow-hidden rounded-2xl border border-border bg-surface shadow-2xl";
