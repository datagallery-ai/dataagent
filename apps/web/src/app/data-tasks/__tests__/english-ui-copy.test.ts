import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createTranslator } from "../../../i18n/translate";
import { getWorkspaceResourceNavGroups } from "../session-pane-ui";

const dataTasksRoot = join(process.cwd(), "src/app/data-tasks");

const shellFiles = [
  "page.tsx",
  "data-tasks-app.tsx",
  "data-task-identity.tsx",
  "session-pane-ui.ts",
  "components/chat/SessionConfigBar.tsx",
  "components/chat/DataTaskChatInput.tsx",
  "components/chat/DataTaskWelcome.tsx",
  "components/task-console/TaskConsole.tsx",
  "components/guide/QuickStartGuide.tsx",
  "components/guide/quick-start-guide-state.ts",
];

const allowedChinesePaths = [
  join(process.cwd(), "src/i18n/messages/zh-CN.json"),
];

describe("workbench shell i18n", () => {
  it("keeps runtime shell source free of inline Chinese (use t() + zh-CN.json)", () => {
    const offenders = shellFiles.flatMap((file) => {
      const content = readFileSync(join(dataTasksRoot, file), "utf8");
      return content
        .split("\n")
        .map((line, index) => ({ file, line, index: index + 1 }))
        .filter(({ line }) => /[\u4e00-\u9fff]/.test(line))
        .map(({ file, line, index }) => `${file}:${index}: ${line.trim()}`);
    });

    expect(offenders).toEqual([]);
  });

  it("allows Chinese only in the locale dictionary", () => {
    for (const file of allowedChinesePaths) {
      expect(readFileSync(file, "utf8")).toMatch(/[\u4e00-\u9fff]/);
    }
  });

  it("mounts LocaleProvider at the data-tasks page root", () => {
    const source = readFileSync(join(dataTasksRoot, "data-tasks-app.tsx"), "utf8");
    expect(source).toContain("<LocaleProvider>");
    expect(source).toContain('useT()');
    expect(source).toContain('t("sidebar.newTask")');
  });

  it("exposes language toggle in the user bar", () => {
    const source = readFileSync(join(dataTasksRoot, "data-task-identity.tsx"), "utf8");
    expect(source).toContain("LanguageToggle");
    expect(source).toContain('t("userBar.settings")');
  });

  it("resolves workspace resource nav labels through translate", () => {
    const t = createTranslator("en");
    const groups = getWorkspaceResourceNavGroups({
      t,
      workspaceConfig: { db: [], kb: [], mcp: [], skill: [], llm: [] },
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
    expect(groups.map((group) => group.title)).toEqual([
      "Data Sources",
      "Data Link",
      "Knowledge",
      "Agent Tools",
      "Models",
      "Assets",
    ]);
  });
});
