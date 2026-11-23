"use client";

import { useEffect, useState } from "react";
import type { JobDto } from "../../../lib/config-api";
import { useT } from "../../../i18n/locale-context";
import { statusTone } from "../ui-tokens";

export function JobProgressBanner({
  job,
  onCancel,
  onDismiss,
}: {
  job: JobDto | null;
  onCancel?: (jobId: string) => void | Promise<void>;
  onDismiss?: () => void;
}) {
  const t = useT();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!job) return;
    if (job.status === "completed" || job.status === "failed" || job.status === "canceled") {
      const timer = window.setTimeout(() => onDismiss?.(), 4000);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [job, onDismiss]);

  if (!job) return null;

  const statusLabel: Record<JobDto["status"], string> = {
    pending: t("jobs.queued"),
    running: t("common.running"),
    completed: t("common.completed"),
    failed: t("common.failed"),
    canceled: t("common.canceled"),
  };

  const tone =
    job.status === "completed"
      ? statusTone("success")
      : job.status === "failed"
        ? statusTone("error")
        : job.status === "canceled"
          ? statusTone("muted")
          : statusTone("info");

  return (
    <div className={`rounded-xl border px-4 py-3 ${tone}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold">
            {job.type === "datasource-introspect" ? t("jobs.schemaSync") : t("jobs.indexRebuild")}
          </div>
          <div className="mt-1 text-xs opacity-80">
            {statusLabel[job.status]} · {job.progress}%
          </div>
          {job.result ? (
            <pre className="mt-2 max-h-24 overflow-auto rounded border border-border/60 bg-surface/70 p-2 text-[11px] leading-4">
              {JSON.stringify(job.result, null, 2)}
            </pre>
          ) : null}
        </div>
        <div className="flex shrink-0 gap-2">
          {(job.status === "pending" || job.status === "running") && onCancel ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setBusy(true);
                void Promise.resolve(onCancel(job.id)).finally(() => setBusy(false));
              }}
              className="rounded-lg border border-current/20 px-2 py-1 text-xs font-medium"
            >
              {t("common.cancel")}
            </button>
          ) : null}
          {(job.status === "completed" ||
            job.status === "failed" ||
            job.status === "canceled") &&
          onDismiss ? (
            <button
              type="button"
              onClick={onDismiss}
              className="rounded-lg border border-current/20 px-2 py-1 text-xs font-medium"
            >
              {t("common.close")}
            </button>
          ) : null}
        </div>
      </div>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-surface/60">
        <div
          className="h-full rounded-full bg-current transition-all"
          style={{ width: `${Math.max(0, Math.min(100, job.progress))}%` }}
        />
      </div>
    </div>
  );
}
