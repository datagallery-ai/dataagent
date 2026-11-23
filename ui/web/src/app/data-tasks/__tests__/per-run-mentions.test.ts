import { describe, expect, it } from "vitest";
import {
  buildMentionResources,
  buildRunConfig,
  buildRunForwardedProps,
  buildAgentRunStatePatch,
  mergeRunForwardedPropsWithCommand,
  countPerRunMentions,
  createChatSession,
  emptyPerRunSelection,
  filterWorkspaceAssetFiles,
  fileMentionFromArtifact,
  removePerRunMention,
  resolveActiveDatasourceId,
  setLiveMentionSupport,
  togglePerRunFileMention,
  togglePerRunMention,
  toggleSessionResource,
  type FileMentionResource,
  type WorkspaceConfigStore,
} from "../data-task-state";

function item(
  id: string,
  name = id,
  status: "connected" | "failed" | "untested" = "connected",
) {
  return { id, name, description: `${name} desc`, enabled: true, status };
}

const store: WorkspaceConfigStore = {
  db: [item("db-default"), item("db-orders")],
  kb: [item("kb-docs")],
  mcp: [item("mcp-fs")],
  skill: [item("skill-a"), item("skill-b")],
  llm: [item("llm-1")],
};

const session = createChatSession();

describe("per-run mention selection", () => {
  it("toggles ids on and off per kind without touching others", () => {
    let selection = emptyPerRunSelection();
    selection = togglePerRunMention(selection, "db", "db-orders");
    selection = togglePerRunMention(selection, "skill", "skill-b");
    expect(selection.db).toEqual(["db-orders"]);
    expect(selection.skill).toEqual(["skill-b"]);
    expect(countPerRunMentions(selection)).toBe(2);

    selection = togglePerRunMention(selection, "db", "db-orders");
    expect(selection.db).toEqual([]);
    expect(countPerRunMentions(selection)).toBe(1);
  });

  it("removePerRunMention is idempotent", () => {
    const selection = togglePerRunMention(
      emptyPerRunSelection(),
      "kb",
      "kb-docs",
    );
    const removed = removePerRunMention(selection, "kb", "kb-docs");
    expect(removed.kb).toEqual([]);
    expect(removePerRunMention(removed, "kb", "kb-docs").kb).toEqual([]);
  });

  it("lists only session-enabled resources for @ picker", () => {
    setLiveMentionSupport({ db: true, kb: true, mcp: true, skill: true });
    const narrowed = toggleSessionResource(session, "db", "db-orders");
    const resources = buildMentionResources(store, narrowed);
    expect(resources).toHaveLength(5); // db(1)+kb(1)+mcp(1)+skill(2)
    expect(resources.some((r) => r.id === "db-orders")).toBe(false);
    const db = resources.find((r) => r.id === "db-default");
    const kb = resources.find((r) => r.id === "kb-docs");
    expect(db?.backendSupported).toBe(true);
    expect(kb?.backendSupported).toBe(true);
    expect(resources.some((r) => r.kind === "llm")).toBe(false);
  });
});

describe("buildRunForwardedProps", () => {
  it("wraps datasourceId and run_config for CopilotKit forwardedProps", () => {
    const runConfig = buildRunConfig(store, {
      activeLlmId: "llm-1",
      defaultDatasourceId: "db-default",
      session,
    });
    expect(buildRunForwardedProps("db-default", runConfig)).toEqual({
      datasourceId: "db-default",
      run_config: runConfig,
    });
  });

  it("merges resume command without dropping run_config", () => {
    const base = buildRunForwardedProps("db-default", {
      activeLlmProfileId: "llm-2",
      activeDatasourceId: "db-default",
      enabledDatasourceIds: ["db-default"],
      enabledKnowledgeIds: [],
      enabledMcpServerIds: [],
      enabledSkillIds: [],
      mentioned: emptyPerRunSelection(),
      fileIds: [],
      pinnedPaths: [],
      evidenceRefs: [],
    });
    expect(
      mergeRunForwardedPropsWithCommand(base, { resume: { action: "approved" } }),
    ).toEqual({
      ...base,
      command: { resume: { action: "approved" } },
    });
  });

  it("patches agent state with latest run_config", () => {
    const forwarded = buildRunForwardedProps("db-default", {
      activeLlmProfileId: "llm-2",
      activeDatasourceId: "db-default",
      enabledDatasourceIds: ["db-default"],
      enabledKnowledgeIds: [],
      enabledMcpServerIds: [],
      enabledSkillIds: [],
      mentioned: emptyPerRunSelection(),
      fileIds: [],
      pinnedPaths: [],
      evidenceRefs: [],
    });
    expect(
      buildAgentRunStatePatch(forwarded, {
        run_config: { activeLlmProfileId: "llm-1" },
        messages: [],
      }),
    ).toMatchObject({
      messages: [],
      datasourceId: "db-default",
      run_config: forwarded.run_config,
    });
  });
});

describe("buildRunConfig", () => {
  it("sends session-enabled ids when there is no @ selection", () => {
    const config = buildRunConfig(store, {
      activeLlmId: "llm-1",
      defaultDatasourceId: "db-default",
      session,
    });
    expect(config.enabledDatasourceIds).toEqual(["db-default", "db-orders"]);
    expect(config.enabledSkillIds).toEqual(["skill-a", "skill-b"]);
    expect(config.activeDatasourceId).toBe("db-default");
    expect(config.activeLlmProfileId).toBe("llm-1");
    expect(config.mentioned).toEqual(emptyPerRunSelection());
  });

  it("keeps enabled*Ids as full session set while @ sets active/mentioned only", () => {
    let selection = emptyPerRunSelection();
    selection = togglePerRunMention(selection, "db", "db-orders");
    selection = togglePerRunMention(selection, "skill", "skill-b");
    const config = buildRunConfig(store, {
      activeLlmId: "llm-1",
      defaultDatasourceId: "db-default",
      session,
      perRunSelection: selection,
    });
    // enabled stays full session set — @ does not narrow availability
    expect(config.enabledDatasourceIds).toEqual(["db-default", "db-orders"]);
    expect(config.enabledSkillIds).toEqual(["skill-a", "skill-b"]);
    expect(config.enabledKnowledgeIds).toEqual(["kb-docs"]);
    // active + mentioned reflect @ picks
    expect(config.activeDatasourceId).toBe("db-orders");
    expect(config.activeSkillId).toBe("skill-b");
    expect(config.mentioned.db).toEqual(["db-orders"]);
    expect(config.mentioned.skill).toEqual(["skill-b"]);
  });

  it("narrows enabled*Ids when session disables resources", () => {
    const narrowed = toggleSessionResource(session, "db", "db-default");
    const config = buildRunConfig(store, {
      activeLlmId: "llm-1",
      defaultDatasourceId: "db-default",
      session: narrowed,
    });
    expect(config.enabledDatasourceIds).toEqual(["db-orders"]);
    expect(config.activeDatasourceId).toBe("db-orders");
  });

  it("excludes unusable datasources from run config and active fallback", () => {
    const mixedStore: WorkspaceConfigStore = {
      ...store,
      db: [
        item("db-failed", "Failed db", "failed"),
        item("db-default"),
        item("db-untested", "Untested db", "untested"),
      ],
    };
    const selection = togglePerRunMention(
      emptyPerRunSelection(),
      "db",
      "db-failed",
    );
    const config = buildRunConfig(mixedStore, {
      activeLlmId: "llm-1",
      defaultDatasourceId: "db-failed",
      session,
      perRunSelection: selection,
    });
    expect(config.enabledDatasourceIds).toEqual(["db-default"]);
    expect(config.activeDatasourceId).toBe("db-default");
    expect(config.mentioned.db).toEqual([]);
    expect(
      resolveActiveDatasourceId(
        mixedStore,
        session,
        selection,
        "db-failed",
      ),
    ).toBe("db-default");
  });

  it("splits file mentions into workspace fileIds and session pinnedPaths", () => {
    const files: FileMentionResource[] = [
      {
        id: "workspace:file-ref-1",
        fileId: "file-ref-1",
        name: "shared.csv",
        description: "工作区文件",
        scope: "workspace",
        backendSupported: true,
      },
      {
        id: "session:artifact-1",
        fileId: "artifact-file-ref",
        name: "report.html",
        description: "本对话产物",
        scope: "session",
        path: "output/report.html",
        backendSupported: true,
      },
    ];
    let fileSelection = togglePerRunFileMention(
      { fileIds: [], pinnedPaths: [] },
      files[0],
    );
    fileSelection = togglePerRunFileMention(fileSelection, files[1]);

    const config = buildRunConfig(store, {
      activeLlmId: "llm-1",
      defaultDatasourceId: "db-default",
      session,
      perRunFiles: fileSelection,
    });

    expect(config.fileIds).toEqual(["file-ref-1"]);
    expect(config.pinnedPaths).toEqual(["output/report.html"]);
  });

  it("carries deduped evidence refs as run focus", () => {
    const evidenceRef = {
      id: "artifact:orders",
      kind: "table" as const,
      label: "orders_by_region",
      sessionId: "thread-1",
      runId: "run-1",
      source: { artifactId: "orders" },
    };
    const config = buildRunConfig(store, {
      activeLlmId: "llm-1",
      defaultDatasourceId: "db-default",
      evidenceRefs: [evidenceRef, evidenceRef],
      session,
    });

    expect(config.evidenceRefs).toEqual([evidenceRef]);
  });

  it("marks current-session artifact pins as backend supported", () => {
    const mention = fileMentionFromArtifact({
      id: "artifact-1",
      kind: "file",
      type: "file",
      title: "report.html",
      summary: "本对话产物",
      fileId: "file-ref-1",
      detail: {
        type: "file",
        path: "output/report.html",
      },
    });

    expect(mention).toMatchObject({
      id: "session:artifact-1",
      fileId: "file-ref-1",
      path: "output/report.html",
      backendSupported: true,
    });
  });
});

describe("workspace file filtering", () => {
  it("keeps only cross-session upload/workspace refs for the workspace asset list", () => {
    const files = filterWorkspaceAssetFiles([
      { id: "workspace", filename: "shared.csv", source: "workspace" },
      { id: "upload", filename: "uploaded.csv", source: "upload" },
      { id: "artifact", filename: "report.html", source: "artifact" },
      { id: "session-upload", filename: "local.txt", source: "upload", sessionId: "s1" },
    ]);

    expect(files.map((file) => file.id)).toEqual(["workspace", "upload"]);
  });
});

describe("resolveActiveDatasourceId", () => {
  it("uses the first valid @db within session-enabled set, else fallback", () => {
    const selection = togglePerRunMention(
      emptyPerRunSelection(),
      "db",
      "db-orders",
    );
    expect(
      resolveActiveDatasourceId(store, session, selection, "db-default"),
    ).toBe("db-orders");
    expect(
      resolveActiveDatasourceId(
        store,
        session,
        emptyPerRunSelection(),
        "db-default",
      ),
    ).toBe("db-default");
  });

  it("ignores @db mentions disabled in session", () => {
    const narrowed = toggleSessionResource(session, "db", "db-orders");
    const selection = togglePerRunMention(
      emptyPerRunSelection(),
      "db",
      "db-orders",
    );
    expect(
      resolveActiveDatasourceId(store, narrowed, selection, "db-default"),
    ).toBe("db-default");
  });

  it("returns undefined when no datasource is enabled in the session", () => {
    const noDbSession = toggleSessionResource(
      toggleSessionResource(session, "db", "db-default"),
      "db",
      "db-orders",
    );
    expect(
      resolveActiveDatasourceId(
        store,
        noDbSession,
        emptyPerRunSelection(),
        "db-default",
      ),
    ).toBeUndefined();
  });
});

describe("buildRunConfig without datasources", () => {
  it("omits activeDatasourceId when the session has no enabled db", () => {
    const noDbSession = toggleSessionResource(
      toggleSessionResource(session, "db", "db-default"),
      "db",
      "db-orders",
    );
    const config = buildRunConfig(store, {
      activeLlmId: "llm-1",
      defaultDatasourceId: "db-default",
      session: noDbSession,
    });
    expect(config.enabledDatasourceIds).toEqual([]);
    expect(config.activeDatasourceId).toBeUndefined();
    expect(buildRunForwardedProps(undefined, config)).toEqual({
      run_config: config,
    });
  });
});
