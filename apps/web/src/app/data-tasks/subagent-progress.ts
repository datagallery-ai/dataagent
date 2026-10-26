import type { TranslateFn } from "../../i18n/types";

type ProgressStatus = "running" | "completed" | "failed" | "suspended";

export type SubagentStep = {
  id: string;
  subagentRunId: string;
  name: string;
  status: ProgressStatus;
  /** Monotonic client time, used only for this live session. */
  startedAtMs: number;
  durationMs?: number;
};

export type SubagentProgress = {
  agentName: string;
  runs: Array<{ id: string; name: string; status: ProgressStatus }>;
  nextStepId: number;
  updatedAtMs: number;
  status: ProgressStatus;
  steps: SubagentStep[];
};

export function finishSubagentProgress(progress: SubagentProgress, failed: boolean): SubagentProgress {
  const now = performance.now();
  return {
    ...progress,
    status: failed ? "failed" : progress.status === "running" ? "completed" : progress.status,
    runs: progress.runs.map((run) =>
      run.status === "running" ? { ...run, status: failed ? "failed" : "completed" } : run,
    ),
    steps: progress.steps.map((step) =>
      step.status === "running"
        ? { ...step, status: failed ? "failed" : "completed", durationMs: Math.max(0, now - step.startedAtMs) }
        : step,
    ),
  };
}

/** Consume native AG-UI lifecycle and step events without a second wire protocol. */
export function updateSubagentProgress(
  current: SubagentProgress | undefined,
  event: { type?: string; [key: string]: unknown },
): SubagentProgress | undefined {
  const { type, subagentRunId: id, stepName } = event;
  if (typeof id !== "string" || !id) return undefined;
  const now = performance.now();
  const run = current?.runs.find((item) => item.id === id);
  if (type === "SUBAGENT_STARTED") {
    if (run || typeof event.name !== "string" || !event.name) return undefined;
    return {
      agentName: current?.agentName ?? event.name,
      runs: [...(current?.runs ?? []), { id, name: event.name, status: "running" }],
      nextStepId: current?.nextStepId ?? 0,
      updatedAtMs: now,
      status: "running",
      steps: current?.steps ?? [],
    };
  }
  if (!current || !run || run.status !== "running") return undefined;
  let { steps, runs, nextStepId } = current;
  const active = steps.find(
    (step) => step.subagentRunId === id && step.name === stepName && step.status === "running",
  );
  if (type === "STEP_STARTED") {
    if (active || typeof stepName !== "string" || !stepName) return undefined;
    steps = [
      ...steps,
      { id: String(nextStepId++), subagentRunId: id, name: stepName, status: "running", startedAtMs: now },
    ];
  } else if (type === "STEP_FINISHED") {
    if (!active) return undefined;
    steps = steps.map((step) =>
      step === active
        ? { ...step, status: "completed", durationMs: Math.max(0, now - step.startedAtMs) }
        : step,
    );
  } else if (type === "SUBAGENT_FINISHED" || type === "SUBAGENT_ERROR") {
    const suspended =
      event.outcome &&
      typeof event.outcome === "object" &&
      "type" in event.outcome &&
      event.outcome.type === "suspended";
    const status = type === "SUBAGENT_ERROR" ? "failed" : suspended ? "suspended" : "completed";
    runs = runs.map((item) => (item.id === id ? { ...item, status } : item));
    steps = steps.map((step) =>
      step.subagentRunId === id && step.status === "running"
        ? { ...step, status, durationMs: Math.max(0, now - step.startedAtMs) }
        : step,
    );
  } else {
    return undefined;
  }
  const status = runs.some((item) => item.status === "running")
    ? "running"
    : runs.some((item) => item.status === "failed")
      ? "failed"
      : runs.some((item) => item.status === "suspended")
        ? "suspended"
        : "completed";
  return { ...current, runs, steps: steps.slice(-100), nextStepId, updatedAtMs: now, status };
}

export function subagentStepLabel(name: string, t: TranslateFn): string {
  const key = `chat.subagentStep.${name}`;
  const translated = t(key);
  if (translated !== key) return translated;
  return name.replace(/[\u0000-\u001f\u007f]/gu, "").replace(/_/gu, " ");
}

/** Compact history after the parent tool settles. The live line stays on the running tool. */
export function subagentSettledTrail(progress: SubagentProgress, t: TranslateFn): string | undefined {
  if (progress.steps.length === 0) return undefined;
  const counts = new Map<string, number>();
  for (const step of progress.steps) {
    const key = `${step.subagentRunId}\0${step.name}`;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const seen = new Set<string>();
  const parts: string[] = [];
  for (const step of progress.steps) {
    const key = `${step.subagentRunId}\0${step.name}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const runName = progress.runs.find((run) => run.id === step.subagentRunId)?.name;
    const label = subagentStepLabel(step.name, t);
    const titled = runName && runName !== progress.agentName ? `${runName} ${label}` : label;
    const count = counts.get(key) ?? 1;
    parts.push(count > 1 ? `${titled} (${count})` : titled);
  }
  if (parts.length === 0) return undefined;
  const tail =
    progress.status === "failed"
      ? t("chat.subagentError")
      : progress.status === "suspended"
        ? t("chat.subagentSuspended")
        : "";
  const body = `${progress.agentName} · ${parts.join(", ")}`;
  return tail ? `${body} · ${tail}` : body;
}

export function subagentProgressText(progress: SubagentProgress, t: TranslateFn): string {
  const step = [...progress.steps].reverse().find((item) => item.status === "running");
  const agent = progress.runs.find((item) => item.status === "running")?.name ?? progress.agentName;
  if (!step) {
    const detail =
      progress.status === "failed"
        ? t("chat.subagentError")
        : progress.status === "suspended"
          ? t("chat.subagentSuspended")
          : progress.status === "completed"
            ? t("chat.subagentReturning")
            : t("chat.subagentStarting");
    return `${agent} · ${detail}`;
  }
  const attempt = progress.steps
    .slice(0, progress.steps.indexOf(step) + 1)
    .filter((item) => item.name === step.name && item.subagentRunId === step.subagentRunId).length;
  const suffix = attempt > 1 ? ` (${attempt})` : "";
  return `${agent} · ${subagentStepLabel(step.name, t)}${suffix}`;
}
