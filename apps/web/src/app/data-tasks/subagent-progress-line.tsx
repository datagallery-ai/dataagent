"use client";

import React, { useEffect, useState } from "react";
import { useT } from "../../i18n/locale-context";
import { subagentProgressText, subagentSettledTrail, type SubagentProgress } from "./subagent-progress";
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
  return (
    <span className="block truncate text-[11px] leading-4 text-muted-light">
      {subagentProgressText(progress, t)}
      {elapsed !== undefined ? <span className="font-mono"> · {elapsed}s</span> : null}
    </span>
  );
}

export function SubagentSettledTrail({ progress }: { progress: SubagentProgress }) {
  const t = useT();
  const text = subagentSettledTrail(progress, t);
  if (!text) return null;
  return <span className="block truncate text-[11px] leading-4 text-muted-light">{text}</span>;
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
