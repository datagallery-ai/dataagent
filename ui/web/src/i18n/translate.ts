import en from "./messages/en.json";
import zhCN from "./messages/zh-CN.json";
import type { Locale, MessageParams, MessageTree, TranslateFn } from "./types";

const MESSAGE_CATALOG: Record<Locale, MessageTree> = {
  "zh-CN": zhCN as MessageTree,
  en: en as MessageTree,
};

function resolvePath(tree: MessageTree, key: string): string | undefined {
  const parts = key.split(".");
  let current: string | MessageTree | undefined = tree;
  for (const part of parts) {
    if (typeof current !== "object" || current == null || !(part in current)) {
      return undefined;
    }
    current = current[part];
  }
  return typeof current === "string" ? current : undefined;
}

function interpolate(template: string, params?: MessageParams): string {
  if (!params) return template;
  return template.replace(/\{\{(\w+)\}\}/g, (_, name: string) => {
    const value = params[name];
    return value === undefined ? `{{${name}}}` : String(value);
  });
}

export function createTranslator(locale: Locale): TranslateFn {
  const primary = MESSAGE_CATALOG[locale];
  const fallback = MESSAGE_CATALOG.en;

  return (key: string, params?: MessageParams) => {
    const resolved =
      resolvePath(primary, key) ?? resolvePath(fallback, key) ?? key;
    return interpolate(resolved, params);
  };
}

export function getMessages(locale: Locale): MessageTree {
  return MESSAGE_CATALOG[locale];
}

export function isLocale(value: string | null | undefined): value is Locale {
  return value === "zh-CN" || value === "en";
}

export function toggleLocaleValue(locale: Locale): Locale {
  return locale === "zh-CN" ? "en" : "zh-CN";
}
