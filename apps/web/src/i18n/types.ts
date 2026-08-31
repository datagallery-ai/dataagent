export type Locale = "zh-CN" | "en";

export type MessageParams = Record<string, string | number>;

export type MessageTree = {
  [key: string]: string | MessageTree;
};

export type TranslateFn = (key: string, params?: MessageParams) => string;

export const LOCALE_STORAGE_KEY = "data-tasks:locale:v1";

export const DEFAULT_LOCALE: Locale = "zh-CN";

export const SUPPORTED_LOCALES: Locale[] = ["zh-CN", "en"];
