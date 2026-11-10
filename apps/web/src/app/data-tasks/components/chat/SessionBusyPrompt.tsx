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
      className="pointer-events-auto mb-2 rounded-xl bg-surface px-3.5 py-3 text-xs leading-5 text-muted shadow-[var(--shadow-float)]"
    >
      <div id="session-busy-title" className="flex items-center gap-2 text-[13px] font-medium text-foreground">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-warn" aria-hidden />
        {t("chatInput.sessionBusyTitle")}
      </div>
      <p className="mt-1 text-[12.5px] text-muted">{t("chatInput.sessionBusyBody")}</p>
      {preview ? (
        <p className="mt-2 rounded-lg bg-surface-subtle px-2.5 py-1.5 text-[12px] leading-5 text-muted">
          {t("chatInput.sessionBusyPreview", { preview })}
        </p>
      ) : null}
      <div className="mt-3 flex flex-wrap gap-1.5">
        <button
          type="button"
          disabled={disabled}
          onClick={onWait}
          className="h-7 cursor-pointer rounded-lg px-2.5 text-[12px] font-medium text-foreground ring-1 ring-border-strong transition-colors hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busyAction === "waiting"
            ? t("chatInput.sessionBusyWaiting")
            : t("chatInput.sessionBusyWait")}
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={onInterrupt}
          className="h-7 cursor-pointer rounded-lg bg-primary px-2.5 text-[12px] font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busyAction === "interrupting"
            ? t("chatInput.sessionBusyInterrupting")
            : t("chatInput.sessionBusyInterrupt")}
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={onDismiss}
          className="h-7 cursor-pointer rounded-lg px-2.5 text-[12px] text-muted transition-colors hover:bg-surface-hover hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
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
      className="pointer-events-auto mb-2 rounded-lg bg-surface-subtle px-3 py-2 text-xs leading-5 text-muted ring-1 ring-border"
    >
      <div className="flex items-center gap-2 font-medium text-foreground">
        <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-foreground" aria-hidden />
        {t("chatInput.sessionRemoteBusyBanner")}
      </div>
      {preview ? (
        <div className="mt-1 text-muted-light">
          {t("chatInput.sessionBusyPreview", { preview })}
        </div>
      ) : null}
    </div>
  );
}
