import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createTranslator } from "../../../i18n/translate";
import {
  getCollapsedWorkspaceRailCopy,
  getCollapsedWorkspacePreviewClassNames,
  getSessionListItemIconSlots,
  getWorkspaceResourceNavGroups,
  isSessionRunActive,
} from "../session-pane-ui";
import type { WorkspaceConfigStore } from "../data-task-state";
import { LEFT_PANEL_MAX_WIDTH } from "../workspace-layout";

function item(id: string, name = id, status: "connected" | "failed" | "untested" = "connected") {
  return { id, name, description: `${name} desc`, enabled: true, status };
}

const workspaceConfig: WorkspaceConfigStore = {
  db: [item("api-duckdb-demo", "API DuckDB Demo")],
  kb: [],
  mcp: [
    item("mcp-fs", "Filesystem MCP"),
    {
      ...item("datalink", "datalink"),
      settings: { toolNames: "datalink_show, datalink_explore, add_table" },
    },
  ],
  skill: [item("skill-default", "Data Skill")],
  llm: [item("llm-default", "GPT-4.1")],
};

const t = createTranslator("en");

const dataTasksPageSource = () =>
  readFileSync(join(process.cwd(), "src/app/data-tasks/data-tasks-app.tsx"), "utf8");
const schemaPreviewSource = () =>
  readFileSync(
    join(process.cwd(), "src/app/data-tasks/components/SchemaBrowserPanel.tsx"),
    "utf8",
  );

describe("session pane ui conventions", () => {
  it("names the collapsed sidebar as a workspace quick rail", () => {
    expect(getCollapsedWorkspaceRailCopy(t)).toEqual({
      expandLabel: "Expand workspace rail",
      railLabel: "Workspace rail",
      sessionCountLabel: "Sessions",
    });
  });

  it("keeps the chat type icon on the left and pinned state on the right", () => {
    expect(getSessionListItemIconSlots({ pinned: true })).toEqual({
      leading: "session",
      trailing: "pin",
    });
    expect(getSessionListItemIconSlots({ pinned: false })).toEqual({
      leading: "session",
      trailing: "none",
    });
    expect(getSessionListItemIconSlots({ pinned: false, running: true })).toEqual({
      leading: "running",
      trailing: "none",
    });
  });

  it("treats running and suspended runs as active sidebar sessions", () => {
    expect(isSessionRunActive("running")).toBe(true);
    expect(isSessionRunActive("suspended")).toBe(true);
    expect(isSessionRunActive("completed")).toBe(false);
    expect(isSessionRunActive("idle")).toBe(false);
  });

  it("keeps the collapsed hover preview as a compact floating card", () => {
    const classes = getCollapsedWorkspacePreviewClassNames();

    expect(classes.panel).toContain("absolute");
    expect(classes.panel).toContain("w-[256px]");
    expect(classes.panel).not.toContain(`w-[${LEFT_PANEL_MAX_WIDTH}px]`);
    expect(classes.panel).toContain("-translate-x-2");
    expect(classes.panel).toContain("opacity-0");
    expect(classes.panel).toContain("collapsed-sidebar-preview-in");
    expect(classes.panel).toContain("transition-[opacity,transform]");
    expect(classes.panel).toContain("group-hover:translate-x-0");
    expect(classes.panel).toContain("group-hover:opacity-100");
    expect(classes.panel).toContain("before:-left-3");
    expect(classes.panel).toContain("before:w-3");
    expect(classes.panel).toContain("max-h-[min(720px,calc(100vh-24px))]");
    expect(classes.panel).not.toContain(" h-full ");
    expect(classes.panel).not.toContain("h-screen");
    expect(classes.sessionList).toContain("max-h-64");
    expect(classes.sessionList).not.toContain("flex-1");
  });

  it("groups workspace resources into product-level navigation entries", () => {
    const groups = getWorkspaceResourceNavGroups({
      t,
      workspaceConfig,
      workspaceFileCount: 7,
      activeConfigPanel: null,
      activeDataLinkPanel: false,
      activeFilesPanel: false,
      capabilitiesReady: true,
      supportsFiles: true,
      supportsKnowledge: true,
      supportsMcp: true,
      supportsSkills: true,
    });

    expect(groups.map((group) => group.title)).toEqual([
      "Data Sources",
      "Data Link",
      "Knowledge",
      "Agent Tools",
      "Models",
      "Assets",
    ]);
    expect(groups[0]).toMatchObject({
      summary: "1",
      action: { type: "config", panel: "db" },
      active: false,
    });
    expect(groups[1]).toMatchObject({
      title: "Data Link",
      summary: "1",
      action: { type: "datalink" },
      active: false,
    });
    expect(groups[4]).toMatchObject({
      title: "Models",
      summary: "1",
      action: { type: "config", panel: "llm" },
      active: false,
    });
    expect(groups[5]).toMatchObject({
      title: "Assets",
      summary: "7",
      action: { type: "assets" },
      active: false,
    });
    expect(groups[3]).toMatchObject({
      title: "Agent Tools",
      summary: "3",
      action: { type: "config", panel: "mcp" },
    });
  });

  it("counts only runnable data sources in the workspace rail", () => {
    const groups = getWorkspaceResourceNavGroups({
      t,
      workspaceConfig: {
        ...workspaceConfig,
        db: [
          item("sqlite-orders", "Orders SQLite", "connected"),
          item("api-duckdb-demo", "API DuckDB Demo", "failed"),
          item("warehouse", "Warehouse", "untested"),
        ],
      },
      workspaceFileCount: 0,
      activeConfigPanel: null,
      activeDataLinkPanel: false,
      activeFilesPanel: false,
      capabilitiesReady: true,
      supportsFiles: true,
      supportsKnowledge: true,
      supportsMcp: true,
      supportsSkills: true,
    });

    expect(groups[0]).toMatchObject({
      title: "Data Sources",
      summary: "1",
    });
  });

  it("keeps sidebar status minimal for static configuration navigation", () => {
    const groups = getWorkspaceResourceNavGroups({
      t,
      workspaceConfig: { ...workspaceConfig, db: [] },
      workspaceFileCount: 0,
      activeConfigPanel: "kb",
      activeDataLinkPanel: false,
      activeFilesPanel: false,
      capabilitiesReady: true,
      supportsFiles: false,
      supportsKnowledge: false,
      supportsMcp: true,
      supportsSkills: true,
    });

    const dataSources = groups[0];
    const assets = groups[5];
    const knowledge = groups[2];
    expect(dataSources.summary).toBe("0");
    expect(assets).toMatchObject({
      title: "Assets",
      summary: "0",
      statusLabel: "Backend unsupported",
    });
    expect(knowledge.active).toBe(true);
    expect(knowledge.statusLabel).toBe("Backend unsupported");
    expect(groups.map((group) => group.statusLabel).filter(Boolean)).toEqual([
      "Backend unsupported",
      "Backend unsupported",
    ]);
  });

  it("does not force the task console open on initial load", () => {
    const source = dataTasksPageSource();

    expect(source).toContain(
      "const [userRightPanelOpen, setUserRightPanelOpen] = useState(false);",
    );
  });

  it("moves schema preview from db settings into the chat header", () => {
    const source = dataTasksPageSource();

    expect(source).toContain("onPreviewDatasource");
    expect(source).not.toContain("Active data sources");
    expect(source).not.toContain("<SchemaBrowserPanel datasources={items} />");
  });

  it("renders schema preview as a downward popover instead of a full-screen modal", () => {
    const source = schemaPreviewSource();

    expect(source).toContain("top-full");
    expect(source).not.toContain("overlayBackdropClass");
    expect(source).not.toContain("aria-modal");
  });
});
