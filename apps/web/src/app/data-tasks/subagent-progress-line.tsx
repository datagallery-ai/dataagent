"use client";

import React, { useEffect, useState } from "react";
import { useT } from "../../i18n/locale-context";
import {
  isInternalSubagentStep,
  subagentFailureLines,
  subagentFailureText,
  subagentProgressText,
  subagentSettledTrail,
  subagentStepLabel,
  type SubagentProgress,
} from "./subagent-progress";
import type { LiveRun, LiveToolCallRecord } from "./live-run-state";

export function SubagentProgressLine({ progress }: { progress: SubagentProgress }) {
  const t = useT();
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    if (progress.status !== "running") return;
    const timer = window.setInterval(() => setNow(performance.now()), 1000);
    return () => window.clearInterval(timer);
  }, [progress.status, progress.updatedAtMs]);
  const step = [...progress.steps]
    .reverse()
    .find((item) => item.status === "running" && !isInternalSubagentStep(item.name));
  const elapsed =
    progress.status === "running"
      ? Math.max(0, Math.floor((now - (step?.startedAtMs ?? progress.updatedAtMs)) / 1000))
      : undefined;
  const idleSeconds =
    progress.status === "running"
      ? Math.max(0, Math.floor((now - progress.updatedAtMs) / 1000))
      : 0;
  const failed = progress.status === "failed";
  const agent = progress.runs.find((item) => item.status === "running")?.name ?? progress.agentName;
  const reason = failed ? subagentFailureText(progress, agent) : undefined;
  return (
    <span className={["block text-[11px] leading-4", failed ? "text-danger" : "text-muted-light"].join(" ")}>
      <span className="flex min-w-0 items-baseline gap-1.5">
        <span className="min-w-0 truncate">{subagentProgressText(progress, t)}</span>
        {elapsed !== undefined ? (
          <span className="shrink-0 font-mono tabular">
            {t("chat.subagentStepElapsed")} {clockLabel(elapsed)}
          </span>
        ) : null}
        {idleSeconds >= 30 ? (
          <span className="shrink-0">
            {clockLabel(idleSeconds)} {t("chat.subagentIdle")}
          </span>
        ) : null}
      </span>
      {reason ? <span className="block truncate">{reason}</span> : null}
    </span>
  );
}

function clockLabel(seconds: number): string {
  if (seconds < 1) return "<1s";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

export function SubagentSettledTrail({ progress }: { progress: SubagentProgress }) {
  const t = useT();
  const text = subagentSettledTrail(progress, t);
  if (!text) return null;
  const failed = progress.status === "failed";
  return (
    <span className={["block truncate text-[11px] leading-4", failed ? "text-danger" : "text-muted-light"].join(" ")}>
      {text}
    </span>
  );
}

export function subagentDetailAvailable(progress: SubagentProgress): boolean {
  return (
    progress.steps.some((step) => !isInternalSubagentStep(step.name)) ||
    subagentFailureLines(progress).length > 0
  );
}

export function SubagentStepHistory({ progress }: { progress: SubagentProgress }) {
  const t = useT();
  const steps = progress.steps.filter((step) => !isInternalSubagentStep(step.name));
  const failures = subagentFailureLines(progress);
  if (steps.length === 0 && failures.length === 0) return null;
  const multiple = new Set(progress.runs.map((run) => run.name)).size > 1;
  return (
    <ul className="mt-1 grid gap-0.5">
      {steps.map((step) => {
        const runName = progress.runs.find((run) => run.id === step.subagentRunId)?.name;
        const titled =
          multiple && runName ? `${runName} ${subagentStepLabel(step.name, t)}` : subagentStepLabel(step.name, t);
        const mark =
          step.status === "completed" ? "✓" : step.status === "failed" ? "✗" : step.status === "suspended" ? "–" : "·";
        const duration =
          step.durationMs !== undefined
            ? `${(step.durationMs / 1000).toFixed(1)}s`
            : step.status === "running"
              ? t("common.running")
              : "";
        return (
          <li key={step.id} className="truncate font-mono text-[10px] leading-4 text-muted-light">
            {mark} {titled}
            {duration ? ` · ${duration}` : ""}
          </li>
        );
      })}
      {failures.map((failure, index) => (
        <li key={`${index}-${failure}`} className="truncate text-[10px] leading-4 text-danger">
          {failure}
        </li>
      ))}
    </ul>
  );
}

export function SubagentDetailProgress({
  call,
}: {
  call: Pick<LiveToolCallRecord, "status" | "progress">;
}) {
  if (!call.progress) return null;
  if (subagentDetailAvailable(call.progress)) return <SubagentStepHistory progress={call.progress} />;
  return <SubagentToolProgress call={call} />;
}

export function SubagentToolProgress({
  call,
}: {
  call: Pick<LiveToolCallRecord, "status" | "progress">;
}) {
  if (!call.progress) return null;
  if (call.status === "running") return <SubagentProgressLine progress={call.progress} />;
  return <SubagentSettledTrail progress={call.progress} />;
}

function subagentNotes(toolCallIds: string[], liveRun: LiveRun | null) {
  return toolCallIds.flatMap((id) => {
    const call = liveRun?.toolCalls.find((item) => item.id === id);
    if (!call?.progress) return [];
    if (
      call.status === "running" ||
      call.progress.steps.length > 0 ||
      call.progress.status === "failed" ||
      call.progress.status === "suspended"
    ) {
      return [call];
    }
    return [];
  });
}

function subagentNeedsCaption(call: LiveToolCallRecord): boolean {
  return call.status === "running" || call.progress?.status === "failed" || call.progress?.status === "suspended";
}

/** Steps that belong to one tool capsule, not a sibling list under the step. */
export function SubagentChipBody({
  call,
}: {
  call: Pick<LiveToolCallRecord, "status" | "progress">;
}) {
  if (!call.progress || !subagentDetailAvailable(call.progress)) return null;
  const caption =
    call.status === "running" ||
    call.progress.status === "failed" ||
    call.progress.status === "suspended";
  return (
    <div className="border-t border-border/70 px-2 pb-1.5 pt-1">
      {caption ? <SubagentToolProgress call={call} /> : null}
      <SubagentStepHistory progress={call.progress} />
    </div>
  );
}

export function SubagentProgressNotes({
  toolCallIds,
  liveRun,
  detail = false,
}: {
  toolCallIds: string[];
  liveRun: LiveRun | null;
  /** The parent step is open, so this list is that disclosure. No second chevron. */
  detail?: boolean;
}) {
  const notes = subagentNotes(toolCallIds, liveRun);
  const visible = detail ? notes : notes.filter(subagentNeedsCaption);
  if (visible.length === 0) return null;
  return (
    <div className="grid gap-1">
      {visible.map((call) => (
        <div key={call.id} className="grid gap-0.5">
          {detail && visible.length === 1 && !subagentNeedsCaption(call) ? null : (
            <SubagentToolProgress call={call} />
          )}
          {detail && call.progress ? <SubagentStepHistory progress={call.progress} /> : null}
        </div>
      ))}
    </div>
  );
}
