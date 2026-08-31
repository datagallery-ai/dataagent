import { getEnabledLlmItems, type WorkspaceConfigStore } from "../../data-task-state";
import type { LiveRunStatus } from "../../live-run-state";
import type { TranslateFn } from "../../../../i18n/types";

export const QUICK_START_PROMPT_SEEN_STORAGE_KEY =
  "data-tasks:quick-start:first-prompt-seen:v1";

export const QUICK_START_DISMISSED_STORAGE_KEY =
  "data-tasks:quick-start:dismissed:v1";

export const QUICK_START_EXAMPLE_PROMPT =
  "Query total orders in the last 30 days, grouped by date";

export type QuickStartStepId =
  | "welcome"
  | "resources"
  | "model"
  | "datasource"
  | "query"
  | "send"
  | "console"
  | "output";

export const QUICK_START_STEP_ORDER: QuickStartStepId[] = [
  "welcome",
  "resources",
  "datasource",
  "model",
  "query",
  "send",
  "console",
  "output",
];

export type QuickStartStorage = Pick<Storage, "getItem" | "setItem">;

export type QuickStartReadiness = {
  hasModel: boolean;
  hasDatasource: boolean;
  preferredDatasourceId: string | null;
  canRun: boolean;
};

export type QuickStartStepPresentation = {
  id: QuickStartStepId;
  targetId: string;
  title: string;
  body: string;
  cta: string;
  blocked?: boolean;
};

export function getQuickStartPromptSeenStorageKey(userId?: string | null): string {
  const normalized = userId?.trim();
  return normalized
    ? `${QUICK_START_PROMPT_SEEN_STORAGE_KEY}:${encodeURIComponent(normalized)}`
    : QUICK_START_PROMPT_SEEN_STORAGE_KEY;
}

export function hasSeenQuickStartPrompt(
  storage: QuickStartStorage | null,
  userId?: string | null,
): boolean {
  return storage?.getItem(getQuickStartPromptSeenStorageKey(userId)) === "true";
}

export function markQuickStartPromptSeen(
  storage: QuickStartStorage | null,
  userId?: string | null,
): void {
  storage?.setItem(getQuickStartPromptSeenStorageKey(userId), "true");
}

export function markQuickStartDismissed(storage: QuickStartStorage | null): void {
  storage?.setItem(QUICK_START_DISMISSED_STORAGE_KEY, "true");
}

export function resolveQuickStartReadiness(
  workspaceConfig: WorkspaceConfigStore,
): QuickStartReadiness {
  const enabledLlms = getEnabledLlmItems(workspaceConfig);
  const availableDatasources = workspaceConfig.db.filter(
    (item) => item.enabled !== false,
  );
  const preferredDatasource = availableDatasources[0] ?? null;
  const hasModel = enabledLlms.length > 0;
  const hasDatasource = preferredDatasource != null;

  return {
    hasModel,
    hasDatasource,
    preferredDatasourceId: preferredDatasource?.id ?? null,
    canRun: hasModel && hasDatasource,
  };
}

export function getQuickStartInitialStep(
  _readiness: QuickStartReadiness,
): QuickStartStepId {
  return "welcome";
}

export function resolveQuickStartStep(
  step: QuickStartStepId,
  {
    readiness,
    runStatus,
    hasSubmittedTask = runStatus !== "idle",
    t,
  }: {
    readiness: QuickStartReadiness;
    runStatus: LiveRunStatus;
    hasSubmittedTask?: boolean;
    t: TranslateFn;
  },
): QuickStartStepPresentation {
  switch (step) {
    case "welcome":
      return {
        id: step,
        targetId: "workspace-layout",
        title: t("guide.welcome.title"),
        body: t("guide.welcome.body"),
        cta: t("guide.next"),
      };
    case "resources":
      return {
        id: step,
        targetId: "workspace-resources",
        title: t("guide.resources.title"),
        body: t("guide.resources.body"),
        cta: t("guide.next"),
      };
    case "datasource":
      return {
        id: step,
        targetId: "datasource-config",
        title: t("guide.datasource.title"),
        body: readiness.preferredDatasourceId
          ? t("guide.datasource.bodyReady", { id: readiness.preferredDatasourceId })
          : t("guide.datasource.bodyMissing"),
        cta: readiness.hasDatasource
          ? t("guide.next")
          : t("guide.openDatasourceConfig"),
      };
    case "model":
      return {
        id: step,
        targetId: "model-picker",
        title: readiness.hasModel
          ? t("guide.model.titleReady")
          : t("guide.model.titleMissing"),
        body: readiness.hasModel
          ? t("guide.model.bodyReady")
          : t("guide.model.bodyMissing"),
        cta: readiness.hasModel ? t("guide.next") : t("guide.openModelConfig"),
      };
    case "query":
      return {
        id: step,
        targetId: "chat-input",
        title: t("guide.query.title"),
        body: t("guide.query.body", { prompt: t("welcome.runSqlPrompt") }),
        cta: t("guide.useThisQuery"),
      };
    case "send":
      return {
        id: step,
        targetId: "chat-input",
        title: t("guide.send.title"),
        body: hasSubmittedTask
          ? t("guide.send.bodyReady")
          : t("guide.send.bodyWaiting"),
        cta: hasSubmittedTask ? t("guide.next") : t("guide.waitingForSend"),
        blocked: !hasSubmittedTask,
      };
    case "console":
      return {
        id: step,
        targetId: "run-console",
        title:
          runStatus === "running"
            ? t("guide.console.titleRunning")
            : t("guide.console.titleIdle"),
        body: t("guide.console.body"),
        cta: t("guide.next"),
      };
    case "output":
      return {
        id: step,
        targetId: "run-output",
        title:
          runStatus === "failed"
            ? t("guide.output.titleFailed")
            : t("guide.output.titleReady"),
        body:
          runStatus === "failed"
            ? t("guide.output.bodyFailed")
            : t("guide.output.bodyReady"),
        cta: t("guide.finish"),
      };
  }
}
