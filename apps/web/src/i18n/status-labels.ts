import type { ConfigItemStatus } from "../app/data-tasks/data-task-state";
import type { LiveRun } from "../app/data-tasks/live-run-state";
import type { TranslateFn } from "./types";

export function translateRunStatus(
  status: LiveRun["runStatus"],
  t: TranslateFn,
): string {
  switch (status) {
    case "running":
      return t("common.running");
    case "suspended":
      return t("common.waiting");
    case "completed":
      return t("common.completed");
    case "failed":
      return t("common.failed");
    case "canceled":
      return t("common.canceled");
    default:
      return t("common.idle");
  }
}

export function translateConfigItemStatus(
  status: ConfigItemStatus | undefined,
  t: TranslateFn,
): string {
  if (status === "connected") return t("common.connected");
  if (status === "failed") return t("common.unavailable");
  return t("common.notTested");
}

export const SESSION_RESOURCE_I18N_KEY = {
  db: "resources.dataSource",
  kb: "resources.knowledge",
  mcp: "resources.mcp",
  skill: "resources.skills",
} as const;

export function translateSessionResourceLabel(
  kind: keyof typeof SESSION_RESOURCE_I18N_KEY,
  t: TranslateFn,
): string {
  return t(SESSION_RESOURCE_I18N_KEY[kind]);
}
