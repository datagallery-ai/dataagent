"use client";

import { useLocale } from "./locale-context";

export function LanguageToggle({ compact = false }: { compact?: boolean }) {
  const { locale, toggleLocale, t } = useLocale();
  // Button shows the *current* language; click switches to the other.
  const currentShort =
    locale === "zh-CN"
      ? t("language.chineseShort")
      : t("language.englishShort");
  const nextLabel =
    locale === "zh-CN"
      ? t("language.switchToEnglish")
      : t("language.switchToChinese");

  return (
    <button
      type="button"
      onClick={toggleLocale}
      title={nextLabel}
      aria-label={nextLabel}
      data-locale={locale}
      className={
        compact
          ? "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border bg-surface text-[11px] font-semibold text-muted transition-colors hover:bg-surface-subtle hover:text-foreground"
          : "flex h-8 shrink-0 items-center justify-center rounded-md border border-border bg-surface px-2 text-[11px] font-semibold text-muted transition-colors hover:bg-surface-subtle hover:text-foreground"
      }
    >
      {currentShort}
    </button>
  );
}
