"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useT } from "../../../../i18n/locale-context";
import { useDataTaskIdentity } from "../../data-task-identity";
import type { WorkspaceConfigStore } from "../../data-task-state";
import type { LiveRun } from "../../live-run-state";
import {
  QUICK_START_STEP_ORDER,
  getQuickStartInitialStep,
  hasSeenQuickStartPrompt,
  markQuickStartDismissed,
  markQuickStartPromptSeen,
  resolveQuickStartReadiness,
  resolveQuickStartStep,
  type QuickStartStepId,
} from "./quick-start-guide-state";

type ConfigPanel = "db" | "llm";

type QuickStartGuideProps = {
  workspaceConfig: WorkspaceConfigStore;
  liveRun: Pick<LiveRun, "runStatus">;
  hasSubmittedTask: boolean;
  onOpenConfigPanel: (panel: ConfigPanel) => void;
  onOpenTaskConsole: () => void;
  onUseExampleQuery: (query: string) => void;
};

type TargetRect = {
  height: number;
  left: number;
  top: number;
  width: number;
};

function browserStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function measureTarget(targetId: string): TargetRect | null {
  if (typeof document === "undefined") return null;
  const element = document.querySelector(`[data-guide-id="${targetId}"]`);
  if (!element) return null;
  const rect = element.getBoundingClientRect();
  return {
    height: rect.height,
    left: rect.left,
    top: rect.top,
    width: rect.width,
  };
}

function nextStep(current: QuickStartStepId): QuickStartStepId {
  const index = QUICK_START_STEP_ORDER.indexOf(current);
  return QUICK_START_STEP_ORDER[
    Math.min(QUICK_START_STEP_ORDER.length - 1, index + 1)
  ];
}

function previousStep(current: QuickStartStepId): QuickStartStepId {
  const index = QUICK_START_STEP_ORDER.indexOf(current);
  return QUICK_START_STEP_ORDER[Math.max(0, index - 1)];
}

export function QuickStartGuide({
  workspaceConfig,
  liveRun,
  hasSubmittedTask,
  onOpenConfigPanel,
  onOpenTaskConsole,
  onUseExampleQuery,
}: QuickStartGuideProps) {
  const t = useT();
  const { scopeKey } = useDataTaskIdentity();
  const readiness = useMemo(
    () => resolveQuickStartReadiness(workspaceConfig),
    [workspaceConfig],
  );
  const [open, setOpen] = useState(false);
  const [stepId, setStepId] = useState<QuickStartStepId>(() =>
    getQuickStartInitialStep(readiness),
  );
  const [targetRect, setTargetRect] = useState<TargetRect | null>(null);
  const step = resolveQuickStartStep(stepId, {
    readiness,
    runStatus: liveRun.runStatus,
    hasSubmittedTask,
    t,
  });
  const currentStepIndex = QUICK_START_STEP_ORDER.indexOf(stepId);

  useEffect(() => {
    if (open) return;
    setStepId(getQuickStartInitialStep(readiness));
  }, [open, readiness]);

  useEffect(() => {
    const storage = browserStorage();
    if (hasSeenQuickStartPrompt(storage, scopeKey)) return;
    markQuickStartPromptSeen(storage, scopeKey);
    setOpen(true);
  }, [scopeKey]);

  const updateTargetRect = useCallback(() => {
    setTargetRect(measureTarget(step.targetId));
  }, [step.targetId]);

  useEffect(() => {
    if (!open) return;
    updateTargetRect();
    window.addEventListener("resize", updateTargetRect);
    window.addEventListener("scroll", updateTargetRect, true);
    return () => {
      window.removeEventListener("resize", updateTargetRect);
      window.removeEventListener("scroll", updateTargetRect, true);
    };
  }, [open, updateTargetRect]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        markQuickStartDismissed(browserStorage());
        setOpen(false);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open]);

  const close = () => {
    markQuickStartDismissed(browserStorage());
    setOpen(false);
  };

  const advance = () => {
    if (step.blocked) return;
    if (stepId === "datasource" && !readiness.hasDatasource) {
      onOpenConfigPanel("db");
      return;
    }
    if (stepId === "model" && !readiness.hasModel) {
      onOpenConfigPanel("llm");
      return;
    }
    if (stepId === "console") {
      onOpenTaskConsole();
    }
    if (stepId === "output") {
      close();
      return;
    }
    setStepId(nextStep(stepId));
  };

  const useExampleQuery = () => {
    onUseExampleQuery(t("welcome.runSqlPrompt"));
    setStepId("send");
  };

  const primaryAction = stepId === "query" ? useExampleQuery : advance;
  const highlightStyle = targetRect
    ? {
        height: Math.max(36, targetRect.height + 12),
        left: Math.max(8, targetRect.left - 6),
        top: Math.max(8, targetRect.top - 6),
        width: Math.max(36, targetRect.width + 12),
      }
    : undefined;
  const popoverStyle = targetRect
    ? {
        left: Math.min(
          Math.max(16, targetRect.left + Math.min(targetRect.width + 18, 360)),
          Math.max(16, window.innerWidth - 360),
        ),
        top: Math.min(
          Math.max(16, targetRect.top),
          Math.max(16, window.innerHeight - 260),
        ),
      }
    : undefined;

  return (
    <div className="relative shrink-0">
      <button
        type="button"
        onClick={() => {
          markQuickStartPromptSeen(browserStorage(), scopeKey);
          setOpen((value) => !value);
        }}
        className="guide-launcher inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-full border border-border bg-surface text-sm font-semibold text-foreground shadow-[var(--shadow-card)] transition-colors duration-200 hover:bg-surface-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20"
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label={t("guide.open")}
        title={t("guide.open")}
      >
        ?
      </button>

      {open ? (
        <>
          {targetRect ? (
            <div
              className="pointer-events-none fixed z-[70] rounded-xl border border-primary/40 bg-primary/5 shadow-[0_0_0_9999px_rgba(15,15,15,0.18)] guide-highlight-pulse"
              style={highlightStyle}
              aria-hidden
            />
          ) : null}
          <section
            role="dialog"
            aria-label={t("guide.dialog")}
            className="fixed z-[80] w-[min(344px,calc(100vw-2rem))] rounded-xl border border-border bg-surface p-4 text-sm shadow-[0_20px_50px_rgba(15,23,42,0.18)] guide-popover-in max-sm:bottom-4 max-sm:left-4 max-sm:right-4"
            style={popoverStyle}
          >
            <div className="mb-3 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-light">
                  {t("guide.badge")}
                </p>
                <h2 className="mt-1 text-base font-semibold text-foreground">
                  {step.title}
                </h2>
                <p className="mt-1 text-[11px] font-medium text-muted-light">
                  {t("guide.stepOf", {
                    current: currentStepIndex + 1,
                    total: QUICK_START_STEP_ORDER.length,
                  })}
                </p>
              </div>
              <button
                type="button"
                onClick={close}
                className="flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-md text-muted-light transition-colors duration-150 hover:bg-surface-subtle hover:text-foreground"
                aria-label={t("guide.close")}
              >
                ×
              </button>
            </div>
            <p className="leading-6 text-muted">{step.body}</p>
            {stepId === "query" ? (
              <div className="mt-3 rounded-lg border border-border bg-surface-subtle px-3 py-2 font-mono text-xs leading-5 text-foreground">
                {t("welcome.runSqlPrompt")}
              </div>
            ) : null}
            <div className="mt-4 flex items-center justify-between gap-3">
              <button
                type="button"
                onClick={() => setStepId(previousStep(stepId))}
                disabled={stepId === QUICK_START_STEP_ORDER[0]}
                className="h-8 cursor-pointer rounded-lg px-3 text-xs font-medium text-muted transition-colors duration-150 hover:bg-surface-subtle hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
              >
                {t("guide.back")}
              </button>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={close}
                  className="h-8 cursor-pointer rounded-lg px-3 text-xs font-medium text-muted transition-colors duration-150 hover:bg-surface-subtle hover:text-foreground"
                >
                  {t("guide.skip")}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    void primaryAction();
                  }}
                  disabled={step.blocked}
                  aria-disabled={step.blocked ? "true" : undefined}
                  className="h-8 cursor-pointer rounded-lg bg-primary px-3 text-xs font-semibold text-white transition-colors duration-150 hover:bg-primary-light focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/25 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-primary"
                >
                  {step.cta}
                </button>
              </div>
            </div>
          </section>
        </>
      ) : null}
    </div>
  );
}
