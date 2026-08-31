import { createHash } from "node:crypto";

export const MODEL_PROFILE_CONNECTIVITY_PAYLOAD_KEYS = [
  "provider",
  "baseUrl",
  "base_url",
  "modelName",
  "model",
  "model_name",
  "fallbackProfileId"
] as const;

const stringValue = (value: unknown): string | undefined =>
  typeof value === "string" && value.trim() ? value.trim() : undefined;

/** True when persisted model-profile fields that affect provider resolution changed. */
export const modelProfileConnectivityPayloadChanged = (
  current: Record<string, unknown>,
  next: Record<string, unknown>
): boolean =>
  MODEL_PROFILE_CONNECTIVITY_PAYLOAD_KEYS.some((key) => {
    const currentValue = stringValue(current[key]);
    const nextValue = stringValue(next[key]);
    return currentValue !== nextValue;
  });

export const resolveModelProfileSaveStatus = (input: {
  connectivityChanged: boolean;
  credentialsUpdated: boolean;
  currentStatus?: string | undefined;
  explicitStatus?: string | undefined;
  isNew: boolean;
}): string => {
  if (input.explicitStatus) {
    return input.explicitStatus;
  }
  if (input.isNew) {
    return "untested";
  }
  if (input.credentialsUpdated || input.connectivityChanged) {
    return "untested";
  }
  return input.currentStatus ?? "untested";
};

/** Fingerprint server LLM env so server-default connected status can expire when env changes. */
export const llmEnvFingerprint = (env: Record<string, string | undefined>): string => {
  const material = [
    env.LLM_PROVIDER ?? "",
    env.LLM_BASE_URL ?? "",
    env.LLM_MODEL ?? "",
    env.LLM_API_KEY ? "key:set" : "key:missing"
  ].join("\0");
  return createHash("sha256").update(material).digest("hex").slice(0, 16);
};

/**
 * True when server LLM env is complete enough to expose the builtin server-default profile.
 * Requires the same fields needed for a real probe (key, base URL, and model) — not just
 * the defaults that createModelProviderFromEnv would invent.
 */
export const isServerLlmEnvConfigured = (
  env: Record<string, string | undefined> = process.env,
): boolean =>
  Boolean(env.LLM_API_KEY?.trim() && env.LLM_BASE_URL?.trim() && env.LLM_MODEL?.trim());

/**
 * Prefer a connectivity-proven profile for run defaults / active selection.
 * Falls back to the first enabled item when none are connected (caller may still block send).
 */
export const preferConnectedResourceId = (
  items: Array<{ id: string; status: string }>,
): string | undefined =>
  items.find((item) => item.status === "connected")?.id ?? items[0]?.id;

export const serverDefaultConnectionStatus = (input: {
  currentStatus?: string | undefined;
  storedFingerprint?: string | undefined;
  env: Record<string, string | undefined>;
}): string => {
  const fingerprint = llmEnvFingerprint(input.env);
  if (input.currentStatus !== "connected") {
    return input.currentStatus ?? "untested";
  }
  if (!input.storedFingerprint || input.storedFingerprint !== fingerprint) {
    return "untested";
  }
  return "connected";
};
