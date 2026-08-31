import { isConfigItemUsable, type WorkspaceConfigStore } from "./data-task-state";
import type { LiveRunStatus } from "./live-run-state";
import { LEFT_PANEL_MAX_WIDTH } from "./workspace-layout";
import type { TranslateFn } from "../../i18n/types";

function isDatalinkMcpItem(item: WorkspaceConfigStore["mcp"][number]): boolean {
  const name = item.name.toLowerCase();
  const toolNames = item.settings?.toolNames?.toLowerCase() ?? "";
  return name.includes("datalink") || toolNames.includes("datalink_") || toolNames.includes("add_table");
}

export type SessionListIconSlot = "session" | "running" | "pin" | "none";

export function isSessionRunActive(status: LiveRunStatus): boolean {
  return status === "running" || status === "suspended";
}

export type WorkspaceResourceNavAction =
  | { type: "config"; panel: "db" | "kb" | "mcp" | "skill" | "llm" }
  | { type: "assets" }
  | { type: "datalink" };

export type WorkspaceResourceNavGroup = {
  id: "data-sources" | "data-link" | "assets" | "knowledge" | "agent-tools" | "models";
  title: string;
  summary: string;
  icon: "database" | "graph" | "assets" | "book" | "tools" | "models";
  action: WorkspaceResourceNavAction;
  active: boolean;
  statusLabel?: string;
};

export function getWorkspaceResourceNavGroups({
  t,
  workspaceConfig,
  workspaceFileCount,
  activeConfigPanel,
  activeDataLinkPanel,
  activeFilesPanel,
  capabilitiesReady,
  supportsFiles,
  supportsKnowledge,
  supportsMcp,
  supportsSkills,
}: {
  t: TranslateFn;
  workspaceConfig: WorkspaceConfigStore;
  workspaceFileCount: number;
  activeConfigPanel: "db" | "kb" | "mcp" | "skill" | "llm" | null;
  activeDataLinkPanel: boolean;
  activeFilesPanel: boolean;
  capabilitiesReady: boolean;
  supportsFiles: boolean;
  supportsKnowledge: boolean;
  supportsMcp: boolean;
  supportsSkills: boolean;
}): WorkspaceResourceNavGroup[] {
  const backendUnsupported = t("resources.backendUnsupported");
  const assetsUnsupported = capabilitiesReady && !supportsFiles;
  const knowledgeUnsupported = capabilitiesReady && !supportsKnowledge;
  const mcpUnsupported = capabilitiesReady && !supportsMcp;
  const skillsUnsupported = capabilitiesReady && !supportsSkills;
  const agentToolsStatus = mcpUnsupported
    ? skillsUnsupported
      ? backendUnsupported
      : t("resources.mcpUnsupported")
    : skillsUnsupported
      ? t("resources.skillsUnsupported")
      : undefined;

  return [
    {
      id: "data-sources",
      title: t("resources.dataSources"),
      summary: String(workspaceConfig.db.filter(isConfigItemUsable).length),
      icon: "database",
      action: { type: "config", panel: "db" },
      active: activeConfigPanel === "db",
    },
    {
      id: "data-link",
      title: t("resources.dataLink"),
      summary: String(workspaceConfig.mcp.filter(isDatalinkMcpItem).length),
      icon: "graph",
      action: { type: "datalink" },
      active: activeDataLinkPanel,
      statusLabel: mcpUnsupported ? backendUnsupported : undefined,
    },
    {
      id: "knowledge",
      title: t("resources.knowledge"),
      summary: String(workspaceConfig.kb.length),
      icon: "book",
      action: { type: "config", panel: "kb" },
      active: activeConfigPanel === "kb",
      statusLabel: knowledgeUnsupported ? backendUnsupported : undefined,
    },
    {
      id: "agent-tools",
      title: t("resources.agentTools"),
      summary: String(workspaceConfig.mcp.length + workspaceConfig.skill.length),
      icon: "tools",
      action: { type: "config", panel: "mcp" },
      active: activeConfigPanel === "mcp" || activeConfigPanel === "skill",
      statusLabel: agentToolsStatus,
    },
    {
      id: "models",
      title: t("resources.models"),
      summary: String(workspaceConfig.llm.length),
      icon: "models",
      action: { type: "config", panel: "llm" },
      active: activeConfigPanel === "llm",
    },
    {
      id: "assets",
      title: t("resources.assets"),
      summary: String(workspaceFileCount),
      icon: "assets",
      action: { type: "assets" },
      active: activeFilesPanel,
      statusLabel: assetsUnsupported ? backendUnsupported : undefined,
    },
  ];
}

export function getCollapsedWorkspaceRailCopy(t: TranslateFn) {
  return {
    expandLabel: t("sidebar.expandRail"),
    railLabel: t("sidebar.railLabel"),
    sessionCountLabel: t("sidebar.sessions"),
  } as const;
}

export function getCollapsedWorkspacePreviewClassNames() {
  return {
    panel:
      "collapsed-sidebar-preview-in absolute left-full top-0 ml-3 flex w-[256px] max-h-[min(720px,calc(100vh-24px))] -translate-x-2 overflow-visible rounded-2xl border border-border bg-surface-subtle opacity-0 shadow-2xl ring-1 ring-black/5 transition-[opacity,transform] duration-200 ease-out before:absolute before:-left-3 before:top-0 before:h-full before:w-3 before:content-[''] pointer-events-none group-hover:pointer-events-auto group-hover:translate-x-0 group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:translate-x-0 group-focus-within:opacity-100",
    content:
      "flex min-h-0 w-full flex-col overflow-hidden rounded-2xl border border-white/60 bg-surface-subtle",
    sessionList: "max-h-64 overflow-y-auto p-2",
  } as const;
}

export function getSessionListItemIconSlots({
  pinned,
  running = false,
}: {
  pinned: boolean;
  running?: boolean;
}): {
  leading: SessionListIconSlot;
  trailing: SessionListIconSlot;
} {
  return {
    leading: running ? "running" : "session",
    trailing: pinned ? "pin" : "none",
  };
}
