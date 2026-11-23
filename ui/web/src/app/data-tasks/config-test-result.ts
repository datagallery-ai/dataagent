import type { WorkspaceConfigKind } from "./data-task-state";
import { ConfigApiError } from "../../lib/config-api";
import type { TranslateFn } from "../../i18n/types";

export type ConfigTestPresentation = {
  tone: "success" | "error";
  title: string;
  details: string[];
};

function stringValue(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

function humanizeProviderTestMessage(message: string, t: TranslateFn): string {
  const trimmed = message.trim();
  const missing = /^PROVIDER_CONFIG_MISSING:(.+)$/u.exec(trimmed);
  if (missing) {
    const profile = missing[1]?.trim() || "this model profile";
    return t("configPanel.providerConfigMissing", { profile });
  }
  const providerFailed = /^PROVIDER_TEST_FAILED:(.+)$/u.exec(trimmed);
  if (providerFailed) {
    return providerFailed[1]?.trim() || trimmed;
  }
  const datasourceFailed = /^DATASOURCE_TEST_FAILED:(.+)$/u.exec(trimmed);
  if (datasourceFailed) {
    return datasourceFailed[1]?.trim() || trimmed;
  }
  const mcpFailed = /^MCP_TEST_FAILED:(.+)$/u.exec(trimmed);
  if (mcpFailed) {
    return mcpFailed[1]?.trim() || trimmed;
  }
  if (trimmed === "MCP_SERVER_CONFIG_INVALID") {
    return t("configPanel.mcpConfigInvalid");
  }
  if (trimmed === "MCP_STDIO_COMMAND_REQUIRED") {
    return t("configPanel.mcpStdioRequired");
  }
  if (trimmed.startsWith("MCP_SERVER_") || trimmed.startsWith("MCP_STDIO_")) {
    return trimmed.replace(/^MCP_[A-Z0-9_]+:?/u, "").trim() || trimmed;
  }
  if (
    trimmed === "REVISION_CONFLICT"
    || trimmed.startsWith("REVISION_CONFLICT:")
    || trimmed.includes("REVISION_CONFLICT")
  ) {
    return t("configPanel.revisionConflict");
  }
  if (
    trimmed.includes("CONFIG_RESOURCE_NOT_FOUND")
    || trimmed.includes("RESOURCE_NOT_FOUND")
    || /not found/iu.test(trimmed)
  ) {
    return t("configPanel.resourceDeleted");
  }
  return trimmed;
}

export function formatConfigTestResult(
  kind: WorkspaceConfigKind,
  result: Record<string, unknown>,
  t: TranslateFn,
): ConfigTestPresentation {
  if (result.tested === false) {
    const reason =
      stringValue(result.reason) ?? t("configPanel.testProbeUnavailable");
    return {
      tone: "success",
      title: t("configPanel.testSkipped"),
      details: [reason],
    };
  }

  const details: string[] = [];
  const reason = stringValue(result.reason);
  const model = stringValue(result.model);
  const latencyMs = stringValue(result.latencyMs);
  const response = stringValue(result.response);
  const status = stringValue(result.status);

  if (reason) details.push(reason);
  if (kind === "llm" && model && !reason) {
    details.push(t("configPanel.testModelLabel", { model }));
  }
  if (latencyMs) details.push(t("configPanel.testDurationLabel", { ms: latencyMs }));
  if (kind === "llm" && response && !reason) {
    details.push(t("configPanel.testResponseLabel", { response }));
  }
  if (details.length === 0 && status) {
    details.push(t("configPanel.testStatusLabel", { status }));
  }
  if (details.length === 0) details.push(t("configPanel.testCompleted"));

  return { tone: "success", title: t("configPanel.testSucceeded"), details };
}

export function formatConfigTestError(
  error: unknown,
  t: TranslateFn,
): ConfigTestPresentation {
  if (error instanceof ConfigApiError) {
    const raw =
      error.code === "REVISION_CONFLICT" && !error.message.includes("REVISION_CONFLICT")
        ? `REVISION_CONFLICT:${error.message}`
        : error.message || error.code;
    return {
      tone: "error",
      title: t("configPanel.testFailedTitle"),
      details: [humanizeProviderTestMessage(raw, t)],
    };
  }
  const message =
    error instanceof Error ? error.message : t("configPanel.testFailed");
  return {
    tone: "error",
    title: t("configPanel.testFailedTitle"),
    details: [humanizeProviderTestMessage(message, t)],
  };
}
