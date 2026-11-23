"use client";

import { useCallback, useSyncExternalStore, type ReactNode } from "react";
import { useT } from "../../i18n/locale-context";
import {
  applyThemePreference,
  getServerThemeSnapshot,
  getThemeSnapshot,
  subscribeTheme,
  type ThemePreference,
} from "../../lib/theme";

function useThemePreference() {
  const snapshot = useSyncExternalStore(subscribeTheme, getThemeSnapshot, getServerThemeSnapshot);
  const choose = useCallback((next: ThemePreference) => {
    applyThemePreference(next);
  }, []);
  return { pref: snapshot.pref, resolved: snapshot.resolved, choose };
}

/** One-click light/dark switch for tight toolbars. */
export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const t = useT();
  const { resolved, choose } = useThemePreference();
  const label = resolved === "dark" ? t("theme.switchToLight") : t("theme.switchToDark");
  return (
    <button
      type="button"
      onClick={() => choose(resolved === "dark" ? "light" : "dark")}
      title={label}
      aria-label={label}
      data-theme-toggle={resolved}
      className={
        compact
          ? "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-hover hover:text-foreground"
          : "flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted transition-colors hover:bg-surface-hover hover:text-foreground"
      }
    >
      {resolved === "dark" ? <MoonIcon /> : <SunIcon />}
    </button>
  );
}

/** System / light / dark choice for menus and settings. */
export function ThemeSegmented() {
  const t = useT();
  const { pref, choose } = useThemePreference();
  const options: Array<{ value: ThemePreference; label: string; icon: ReactNode }> = [
    { value: "system", label: t("theme.system"), icon: <MonitorIcon /> },
    { value: "light", label: t("theme.light"), icon: <SunIcon /> },
    { value: "dark", label: t("theme.dark"), icon: <MoonIcon /> },
  ];
  return (
    <div className="flex items-center justify-between gap-3 px-2 py-1.5">
      <span className="text-xs text-muted">{t("theme.label")}</span>
      <div role="group" aria-label={t("theme.label")} className="flex gap-0.5 rounded-lg bg-surface-hover p-0.5">
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            onClick={() => choose(option.value)}
            aria-pressed={pref === option.value}
            title={option.label}
            aria-label={option.label}
            className={[
              "flex h-6 w-7 items-center justify-center rounded-md transition-colors",
              pref === option.value
                ? "bg-surface text-foreground shadow-[0_1px_2px_rgb(0_0_0/0.1)]"
                : "text-muted-light hover:text-foreground",
            ].join(" ")}
          >
            {option.icon}
          </button>
        ))}
      </div>
    </div>
  );
}

function SunIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z" />
    </svg>
  );
}

function MonitorIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3.5" y="4.5" width="17" height="12" rx="2" />
      <path d="M9 20h6M12 16.5V20" />
    </svg>
  );
}
