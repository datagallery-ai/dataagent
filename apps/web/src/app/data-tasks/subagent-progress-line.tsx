"use client";

import React, { useEffect, useState } from "react";
import { useT } from "../../i18n/locale-context";
import {
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
  const step = [...progress.steps].reverse().find((item) => item.status === "running");
  const elapsed =
    progress.status === "running"
      ? Math.max(0, Math.floor((now - (step?.startedAtMs ?? progress.updatedAtMs)) / 1000))
      : undefined;
  const idleSeconds =
    progress.status === "running"
      ? Math.max(0, Math.floor((now - progress.updatedAtMs) / 1000))
      : 0;
  return (
    <span className="block truncate text-[11px] leading-4 text-muted-light">
      {subagentProgressText(progress, t)}
      {elapsed !== undefined ? <span className="font-mono"> · {elapsed}s</span> : null}
      {idleSeconds >= 30 ? <span> · {t("chat.subagentWaiting")}</span> : null}
    </span>
  );
}

export function SubagentSettledTrail({ progress }: { progress: SubagentProgress }) {
  const t = useT();
  const text = subagentSettledTrail(progress, t);
  if (!text) return null;
  return <span className="block truncate text-[11px] leading-4 text-muted-light">{text}</span>;
}

export function SubagentStepHistory({ progress }: { progress: SubagentProgress }) {
  const t = useT();
  if (progress.steps.length === 0) return null;
  const multiple = new Set(progress.runs.map((run) => run.name)).size > 1;
  const failure = subagentFailureText(progress);
  return (
    <ul className="mt-1 grid gap-0.5">
      {progress.steps.map((step) => {
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
      {failure ? <li className="truncate text-[10px] leading-4 text-step-error">{failure}</li> : null}
    </ul>
  );
}

export function SubagentToolProgress({
  call,
}: {
  call: Pick<LiveToolCallRecord, "status" | "progress">;
}) {
  if (!call.progress) return null;
  if (call.status === "running") return <SubagentProgressLine progress={call.progress} />;
  if (call.progress.steps.length === 0) return null;
  return <SubagentSettledTrail progress={call.progress} />;
}

export function SubagentProgressNotes({
  toolCallIds,
  liveRun,
}: {
  toolCallIds: string[];
  liveRun: LiveRun | null;
}) {
  const notes = toolCallIds.flatMap((id) => {
    const call = liveRun?.toolCalls.find((item) => item.id === id);
    if (!call?.progress) return [];
    if (call.status === "running" || call.progress.steps.length > 0) return [call];
    return [];
  });
  if (notes.length === 0) return null;
  return (
    <div className="mt-2 grid gap-1">
      {notes.map((call) => (
        <SubagentToolProgress key={call.id} call={call} />
      ))}
    </div>
  );
}
