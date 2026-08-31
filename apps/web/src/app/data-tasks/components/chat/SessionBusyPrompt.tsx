"use client";

import type { SessionActiveRunDto } from "../../../../lib/config-api/types";
import { useT } from "../../../../i18n/locale-context";

type SessionBusyPromptProps = {
  activeRun: SessionActiveRunDto;
  busyAction?: "idle" | "waiting" | "interrupting";
  onWait: () => void;
  onInterrupt: () => void;
  onDismiss: () => void;
};

export function SessionBusyPrompt({
  activeRun,
  busyAction = "idle",
  onWait,
  onInterrupt,
  onDismiss,
}: SessionBusyPromptProps) {
  const t = useT();
  const preview = activeRun.userInputPreview?.trim();
  const disabled = busyAction !== "idle";

  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="session-busy-title"
      data-testid="session-busy-prompt"
      className="pointer-events-auto mb-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-3 text-xs leading-5 text-amber-950 shadow-sm"
    >
      <div id="session-busy-title" className="text-sm font-medium text-amber-950">
        {t("chatInput.sessionBusyTitle")}
      </div>
      <p className="mt-1 text-amber-900/90">{t("chatInput.sessionBusyBody")}</p>
      {preview ? (
        <p className="mt-2 rounded-lg bg-white/70 px-2 py-1.5 text-[11px] leading-4 text-amber-900/80">
          {t("chatInput.sessionBusyPreview", { preview })}
        </p>
      ) : null}
      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          disabled={disabled}
          onClick={onWait}
          className="rounded-lg border border-amber-300 bg-white px-2.5 py-1.5 text-[11px] font-medium text-amber-950 hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busyAction === "waiting"
            ? t("chatInput.sessionBusyWaiting")
            : t("chatInput.sessionBusyWait")}
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={onInterrupt}
          className="rounded-lg border border-amber-800 bg-amber-900 px-2.5 py-1.5 text-[11px] font-medium text-amber-50 hover:bg-amber-800 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busyAction === "interrupting"
            ? t("chatInput.sessionBusyInterrupting")
            : t("chatInput.sessionBusyInterrupt")}
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={onDismiss}
          className="rounded-lg px-2.5 py-1.5 text-[11px] font-medium text-amber-900/80 hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {t("chatInput.sessionBusyDismiss")}
        </button>
      </div>
    </div>
  );
}

type SessionRemoteBusyBannerProps = {
  activeRun: SessionActiveRunDto;
};

export function SessionRemoteBusyBanner({ activeRun }: SessionRemoteBusyBannerProps) {
  const t = useT();
  const preview = activeRun.userInputPreview?.trim();
  return (
    <div
      role="status"
      data-testid="session-remote-busy-banner"
      className="pointer-events-auto mb-2 rounded-xl border border-sky-200 bg-sky-50 px-3 py-2 text-xs leading-5 text-sky-950"
    >
      <div className="font-medium">{t("chatInput.sessionRemoteBusyBanner")}</div>
      {preview ? (
        <div className="mt-1 text-sky-900/80">
          {t("chatInput.sessionBusyPreview", { preview })}
        </div>
      ) : null}
    </div>
  );
}
