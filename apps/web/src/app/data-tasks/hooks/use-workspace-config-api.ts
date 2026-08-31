"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  applyBackendCapabilities,
  configApi,
  ConfigApiError,
  getRuntimeCapabilities,
  itemToCreateBody,
  itemToPatchBody,
  mergeItemFromDto,
  workspaceConfigDtoToStore,
} from "../../../lib/config-api";
import type { DatasourceTypeDto, JobDto, KnowledgeDocumentDto, RunDefaultsDto } from "../../../lib/config-api";
import {
  defaultWorkspaceConfig,
  setLiveBackendCapabilities,
  setLiveDatasourceTypes,
  setLiveMentionSupport,
  setLivePendingCapabilities,
  type ConfigItemStatus,
  type WorkspaceConfigItem,
  type WorkspaceConfigKind,
  type WorkspaceConfigStore,
} from "../data-task-state";
import {
  collectUntestedAutoTestItems,
  runAutoTestsWithConcurrency,
  scheduleDeferredWork,
} from "../workspace-config-bootstrap";

function statusFromTestResult(result: Record<string, unknown>): ConfigItemStatus | null {
  if (result.tested === false) return null;
  const status = typeof result.status === "string" ? result.status.trim() : "";
  if (status === "connected" || status === "ready" || status === "valid") return "connected";
  if (status === "failed" || status === "invalid") return "failed";
  return null;
}

function isNotFoundConfigError(err: unknown): boolean {
  if (!(err instanceof ConfigApiError)) return false;
  return (
    err.code === "RESOURCE_NOT_FOUND"
    || err.message.includes("CONFIG_RESOURCE_NOT_FOUND")
    || /not found/iu.test(err.message)
  );
}

export type WorkspaceApiState = {
  workspaceConfig: WorkspaceConfigStore;
  runDefaults: RunDefaultsDto | null;
  datasourceTypes: DatasourceTypeDto[];
  loading: boolean;
  error: string | null;
  capabilitiesReady: boolean;
};

export type WorkspaceApiActions = {
  refresh: () => Promise<void>;
  patchLocalItem: (
    kind: WorkspaceConfigKind,
    itemId: string,
    patch: Partial<
      Pick<WorkspaceConfigItem, "name" | "description" | "enabled" | "settings">
    >,
  ) => void;
  createItem: (
    kind: WorkspaceConfigKind,
    item: WorkspaceConfigItem,
    skillFile?: File,
  ) => Promise<string>;
  updateItem: (
    kind: WorkspaceConfigKind,
    item: WorkspaceConfigItem,
  ) => Promise<WorkspaceConfigItem>;
  deleteItem: (kind: WorkspaceConfigKind, itemId: string) => Promise<void>;
  testItem: (kind: WorkspaceConfigKind, itemId: string) => Promise<Record<string, unknown>>;
  introspectDatasource: (itemId: string) => Promise<JobDto>;
  reindexKnowledgeBase: (itemId: string) => Promise<JobDto>;
  uploadKnowledgeFile: (itemId: string, file: File) => Promise<void>;
  listKnowledgeFiles: (itemId: string) => Promise<{ documents: KnowledgeDocumentDto[] }>;
  deleteKnowledgeFile: (itemId: string, documentId: string) => Promise<void>;
  reindexKnowledgeFile: (itemId: string, documentId: string) => Promise<KnowledgeDocumentDto>;
  replaceSkillPackage: (itemId: string, file: File) => Promise<void>;
  validateSkill: (itemId: string) => Promise<void>;
  pollJob: (
    jobId: string,
    onUpdate?: (job: JobDto) => void,
  ) => Promise<JobDto>;
  cancelJob: (jobId: string) => Promise<JobDto>;
};

function mentionSupportFromCapabilities(): Record<
  "db" | "kb" | "mcp" | "skill",
  boolean
> {
  const runtime = getRuntimeCapabilities();
  return {
    db: true,
    kb: runtime.knowledge,
    mcp: runtime.mcp,
    skill: runtime.skills,
  };
}

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

export function useWorkspaceConfigApi(): WorkspaceApiState & WorkspaceApiActions {
  const [workspaceConfig, setWorkspaceConfig] = useState<WorkspaceConfigStore>(
    defaultWorkspaceConfig,
  );
  const [runDefaults, setRunDefaults] = useState<RunDefaultsDto | null>(null);
  const [datasourceTypes, setDatasourceTypes] = useState<DatasourceTypeDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [capabilitiesReady, setCapabilitiesReady] = useState(false);

  const applyCapabilities = useCallback(async () => {
    const caps = await configApi.getCapabilities();
    const mapped = applyBackendCapabilities(caps);
    setLiveBackendCapabilities(mapped);
    setLivePendingCapabilities({
      "datasource.extendedTypes": caps["datasource.extendedTypes"] ?? false,
      "datasource.fieldMasking": caps["datasource.fieldMasking"] ?? false,
      "datasource.introspectionPolicy": caps["datasource.introspectionPolicy"] ?? false,
      "datasource.samplePolicy": caps["datasource.samplePolicy"] ?? false,
      "kb.chunking": caps["kb.chunking"] ?? false,
      "kb.citationPolicy": caps["kb.citationPolicy"] ?? false,
      "kb.scope": caps["kb.scope"] ?? false,
      "llm.advancedSampling": caps["llm.advancedSampling"] ?? false,
      "mcp.stdio": caps["mcp.stdio"] ?? false,
      "mcp.toolPolicy": caps["mcp.toolPolicy"] ?? false,
      "skill.resourceBinding": caps["skill.resourceBinding"] ?? false,
    });
    setLiveMentionSupport(mentionSupportFromCapabilities());
    setCapabilitiesReady(true);
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // Fetch capabilities in parallel with workspace payloads to save one RTT
      // on production BFF (password mode) where each hop is expensive.
      const [capsSettled, workspace, defaults, datasourceTypes] = await Promise.all([
        applyCapabilities().then(
          () => ({ ok: true as const }),
          (err: unknown) => ({ ok: false as const, err }),
        ),
        configApi.getWorkspaceConfig(),
        configApi.getRunDefaults(),
        configApi.listDatasourceTypes(),
      ]);
      if (!capsSettled.ok) throw capsSettled.err;
      setLiveDatasourceTypes(datasourceTypes);
      setDatasourceTypes(datasourceTypes);
      setWorkspaceConfig(workspaceConfigDtoToStore(workspace));
      setRunDefaults(defaults);
    } catch (err) {
      const message =
        err instanceof ConfigApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "Failed to load workspace configuration";
      setError(message);
      setWorkspaceConfig(defaultWorkspaceConfig());
      setDatasourceTypes([]);
    } finally {
      setLoading(false);
    }
  }, [applyCapabilities]);

  // Quiet refresh used after connectivity tests so revision/status stay current
  // without flipping the panel into a loading state.
  const refreshQuiet = useCallback(async () => {
    try {
      const [workspace, defaults] = await Promise.all([
        configApi.getWorkspaceConfig(),
        configApi.getRunDefaults(),
      ]);
      setWorkspaceConfig(workspaceConfigDtoToStore(workspace));
      setRunDefaults(defaults);
    } catch {
      // Keep the current local store if a background refresh fails.
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Deduplicate concurrent auto/manual tests for the same item so two probes
  // cannot race on expected_revision and surface REVISION_CONFLICT.
  const inFlightTestsRef = useRef<Map<string, Promise<Record<string, unknown>>>>(new Map());

  const runItemTest = useCallback(
    async (kind: WorkspaceConfigKind, itemId: string): Promise<Record<string, unknown>> => {
      const key = `${kind}:${itemId}`;
      const existing = inFlightTestsRef.current.get(key);
      if (existing) return existing;

      const pending = (async (): Promise<Record<string, unknown>> => {
        if (kind === "db") return configApi.testDatasource(itemId);
        if (kind === "kb") return configApi.testKnowledgeBase(itemId);
        if (kind === "mcp") return configApi.testMcpServer(itemId);
        if (kind === "llm") return configApi.testModelProfile(itemId);
        return configApi.testSkill(itemId);
      })();

      inFlightTestsRef.current.set(key, pending);
      try {
        return await pending;
      } finally {
        if (inFlightTestsRef.current.get(key) === pending) {
          inFlightTestsRef.current.delete(key);
        }
      }
    },
    [],
  );

  // Auto-run connectivity tests for untested config after first paint so LLM/MCP
  // probes (often multi-second) do not block chat interactivity. Bounded
  // concurrency replaces the previous serial await chain.
  const autoTestedRef = useRef<Set<string>>(new Set());
  const autoTestingRef = useRef(false);

  useEffect(() => {
    if (loading || autoTestingRef.current) return;

    const pending = collectUntestedAutoTestItems(workspaceConfig, autoTestedRef.current);
    if (pending.length === 0) return;

    for (const entry of pending) autoTestedRef.current.add(entry.key);
    autoTestingRef.current = true;

    let started = false;
    const cancel = scheduleDeferredWork(() => {
      started = true;
      void (async () => {
        await runAutoTestsWithConcurrency(
          pending,
          async (entry) => {
            try {
              const result = await runItemTest(entry.kind, entry.id);
              const nextStatus = statusFromTestResult(result);
              if (!nextStatus) return;
              setWorkspaceConfig((current) => {
                if (!current[entry.kind].some((item) => item.id === entry.id)) {
                  return current;
                }
                return {
                  ...current,
                  [entry.kind]: current[entry.kind].map((item) =>
                    item.id === entry.id ? { ...item, status: nextStatus } : item,
                  ),
                };
              });
            } catch (err) {
              if (isNotFoundConfigError(err)) {
                return;
              }
              setWorkspaceConfig((current) => {
                if (!current[entry.kind].some((item) => item.id === entry.id)) {
                  return current;
                }
                return {
                  ...current,
                  [entry.kind]: current[entry.kind].map((item) =>
                    item.id === entry.id ? { ...item, status: "failed" as ConfigItemStatus } : item,
                  ),
                };
              });
            }
          },
          2,
        );
        await refreshQuiet();
        autoTestingRef.current = false;
      })();
    });

    return () => {
      cancel();
      if (!started) {
        for (const entry of pending) autoTestedRef.current.delete(entry.key);
        autoTestingRef.current = false;
      }
    };
  }, [loading, workspaceConfig, runItemTest, refreshQuiet]);

  const replaceItemInStore = useCallback(
    (kind: WorkspaceConfigKind, item: WorkspaceConfigItem) => {
      setWorkspaceConfig((current) => ({
        ...current,
        [kind]: current[kind].map((entry) => (entry.id === item.id ? item : entry)),
      }));
    },
    [],
  );

  const formatConfigActionError = useCallback((err: unknown): Error => {
    if (err instanceof ConfigApiError) {
      if (err.code === "SECRET_MASTER_KEY_REQUIRED") {
        return new Error(
          "SECRET_MASTER_KEY is not configured on the server, so API keys cannot be saved. Set it in .env and restart the API.",
        );
      }
      if (err.code === "REVISION_CONFLICT" || err.message.startsWith("REVISION_CONFLICT:")) {
        return new Error(
          "This configuration was updated elsewhere (for example by a connection test). Refresh and try again.",
        );
      }
      if (
        err.code === "RESOURCE_NOT_FOUND" ||
        err.message.includes("CONFIG_RESOURCE_NOT_FOUND")
      ) {
        return new Error(
          "This configuration does not exist on the backend. Create and save it before using it.",
        );
      }
    }
    return err instanceof Error ? err : new Error("Configuration action failed");
  }, []);

  const createItem = useCallback(
    async (
      kind: WorkspaceConfigKind,
      item: WorkspaceConfigItem,
      skillFile?: File,
    ): Promise<string> => {
      try {
        if (kind === "skill") {
          if (!skillFile) {
            throw new Error("Creating a skill requires a SKILL.md or .zip package");
          }
          const form = new FormData();
          form.append("file", skillFile);
          form.append("id", item.id);
          form.append("name", item.name);
          if (item.description.trim()) form.append("description", item.description);
          form.append("defaultEnabled", String(item.enabled));
          const created = await configApi.createSkill(form);
          const mapped = mergeItemFromDto(kind, item, created);
          setWorkspaceConfig((current) => ({
            ...current,
            skill: [...current.skill, mapped],
          }));
          return mapped.id;
        }

        const body = itemToCreateBody(kind, item);
        let createdId = item.id;
        if (kind === "db") {
          const created = await configApi.createDatasource(body);
          createdId = created.id;
          setWorkspaceConfig((current) => ({
            ...current,
            db: [...current.db, mergeItemFromDto(kind, item, created)],
          }));
        } else if (kind === "kb") {
          const created = await configApi.createKnowledgeBase(body);
          createdId = created.id;
          setWorkspaceConfig((current) => ({
            ...current,
            kb: [...current.kb, mergeItemFromDto(kind, item, created)],
          }));
        } else if (kind === "mcp") {
          const created = await configApi.createMcpServer(body);
          createdId = created.id;
          setWorkspaceConfig((current) => ({
            ...current,
            mcp: [...current.mcp, mergeItemFromDto(kind, item, created)],
          }));
        } else if (kind === "llm") {
          const created = await configApi.createModelProfile(body);
          createdId = created.id;
          setWorkspaceConfig((current) => ({
            ...current,
            llm: [...current.llm, mergeItemFromDto(kind, item, created)],
          }));
        }
        return createdId;
      } catch (err) {
        throw formatConfigActionError(err);
      }
    },
    [formatConfigActionError],
  );

  const updateItem = useCallback(
    async (
      kind: WorkspaceConfigKind,
      item: WorkspaceConfigItem,
    ): Promise<WorkspaceConfigItem> => {
      const body = itemToPatchBody(kind, item);
      try {
        if (kind === "db") {
          const updated = await configApi.patchDatasource(item.id, body);
          const merged = mergeItemFromDto(kind, item, updated);
          replaceItemInStore(kind, merged);
          return merged;
        }
        if (kind === "kb") {
          const updated = await configApi.patchKnowledgeBase(item.id, body);
          const merged = mergeItemFromDto(kind, item, updated);
          replaceItemInStore(kind, merged);
          return merged;
        }
        if (kind === "mcp") {
          const updated = await configApi.patchMcpServer(item.id, body);
          const merged = mergeItemFromDto(kind, item, updated);
          replaceItemInStore(kind, merged);
          return merged;
        }
        if (kind === "llm") {
          const updated = await configApi.patchModelProfile(item.id, body);
          const merged = mergeItemFromDto(kind, item, updated);
          replaceItemInStore(kind, merged);
          return merged;
        }
        if (kind === "skill") {
          const updated = await configApi.patchSkill(item.id, body);
          const merged = mergeItemFromDto(kind, item, updated);
          replaceItemInStore(kind, merged);
          return merged;
        }
        return item;
      } catch (err) {
        if (err instanceof ConfigApiError && err.code === "REVISION_CONFLICT") {
          await refresh();
          throw new Error("This configuration was updated elsewhere. The latest version has been loaded; try again.");
        }
        if (
          err instanceof ConfigApiError &&
          (err.code === "RESOURCE_NOT_FOUND" ||
            err.message.includes("CONFIG_RESOURCE_NOT_FOUND"))
        ) {
          await refresh();
          throw new Error(
            "This configuration does not exist on the backend. The list has been refreshed; create it again.",
          );
        }
        if (err instanceof ConfigApiError && err.code === "SECRET_MASTER_KEY_REQUIRED") {
          throw new Error(
            "SECRET_MASTER_KEY is not configured on the server, so API keys cannot be saved. Set it in .env and restart the API.",
          );
        }
        throw err;
      }
    },
    [refresh, replaceItemInStore],
  );

  const deleteItem = useCallback(
    async (kind: WorkspaceConfigKind, itemId: string): Promise<void> => {
      try {
        if (kind === "db") await configApi.deleteDatasource(itemId);
        else if (kind === "kb") await configApi.deleteKnowledgeBase(itemId);
        else if (kind === "mcp") await configApi.deleteMcpServer(itemId);
        else if (kind === "llm") await configApi.deleteModelProfile(itemId);
        else if (kind === "skill") await configApi.deleteSkill(itemId);

        setWorkspaceConfig((current) => ({
          ...current,
          [kind]: current[kind].filter((item) => item.id !== itemId),
        }));
      } catch (err) {
        if (err instanceof ConfigApiError && err.code === "REVISION_CONFLICT") {
          await refreshQuiet();
        }
        throw formatConfigActionError(err);
      }
    },
    [formatConfigActionError, refreshQuiet],
  );

  const testItem = useCallback(
    async (kind: WorkspaceConfigKind, itemId: string): Promise<Record<string, unknown>> => {
      try {
        const result = await runItemTest(kind, itemId);
        await refreshQuiet();
        return result;
      } catch (err) {
        await refreshQuiet();
        if (isNotFoundConfigError(err)) {
          throw new Error(
            "This configuration was deleted while testing. The list has been refreshed.",
          );
        }
        if (
          err instanceof ConfigApiError
          && (err.code === "REVISION_CONFLICT" || err.message.includes("REVISION_CONFLICT"))
        ) {
          // One automatic retry after refresh — common when auto-test and save race.
          try {
            const result = await runItemTest(kind, itemId);
            await refreshQuiet();
            return result;
          } catch (retryErr) {
            if (
              retryErr instanceof ConfigApiError
              && (retryErr.code === "REVISION_CONFLICT" || retryErr.message.includes("REVISION_CONFLICT"))
            ) {
              throw new Error(
                "Configuration was updated while testing. Refresh and try again.",
              );
            }
            throw retryErr;
          }
        }
        throw err;
      }
    },
    [refreshQuiet, runItemTest],
  );

  const introspectDatasource = useCallback(async (itemId: string): Promise<JobDto> => {
    return configApi.introspectDatasource(itemId);
  }, []);

  const reindexKnowledgeBase = useCallback(async (itemId: string): Promise<JobDto> => {
    return configApi.reindexKnowledgeBase(itemId);
  }, []);

  const uploadKnowledgeFile = useCallback(async (itemId: string, file: File): Promise<void> => {
    await configApi.uploadKnowledgeFile(itemId, file);
    await refresh();
  }, [refresh]);

  const listKnowledgeFiles = useCallback(async (itemId: string) => {
    return configApi.listKnowledgeFiles(itemId);
  }, []);

  const deleteKnowledgeFile = useCallback(async (itemId: string, documentId: string): Promise<void> => {
    await configApi.deleteKnowledgeFile(itemId, documentId);
  }, []);

  const reindexKnowledgeFile = useCallback(async (itemId: string, documentId: string) => {
    return configApi.reindexKnowledgeFile(itemId, documentId);
  }, []);

  const replaceSkillPackage = useCallback(async (itemId: string, file: File): Promise<void> => {
    const form = new FormData();
    form.append("file", file);
    const updated = await configApi.replaceSkill(itemId, form);
    setWorkspaceConfig((current) => ({
      ...current,
      skill: current.skill.map((item) =>
        item.id === itemId ? mergeItemFromDto("skill", item, updated) : item,
      ),
    }));
  }, []);

  const validateSkill = useCallback(async (itemId: string): Promise<void> => {
    await configApi.validateSkill(itemId);
    await refresh();
  }, [refresh]);

  const pollJob = useCallback(
    async (jobId: string, onUpdate?: (job: JobDto) => void): Promise<JobDto> => {
      for (let attempt = 0; attempt < 120; attempt += 1) {
        const job = await configApi.getJob(jobId);
        onUpdate?.(job);
        if (
          job.status === "completed" ||
          job.status === "failed" ||
          job.status === "canceled"
        ) {
          return job;
        }
        await sleep(500);
      }
      throw new Error("The job timed out. Check the job list for status later.");
    },
    [],
  );

  const cancelJob = useCallback(async (jobId: string): Promise<JobDto> => {
    return configApi.cancelJob(jobId);
  }, []);

  const patchLocalItem = useCallback(
    (
      kind: WorkspaceConfigKind,
      itemId: string,
      patch: Partial<
        Pick<WorkspaceConfigItem, "name" | "description" | "enabled" | "settings">
      >,
    ) => {
      setWorkspaceConfig((current) => ({
        ...current,
        [kind]: current[kind].map((item) => {
          if (item.id !== itemId) return item;
          return {
            ...item,
            ...patch,
            settings: patch.settings
              ? { ...item.settings, ...patch.settings }
              : item.settings,
          };
        }),
      }));
    },
    [],
  );

  return {
    workspaceConfig,
    runDefaults,
    datasourceTypes,
    loading,
    error,
    capabilitiesReady,
    refresh,
    patchLocalItem,
    createItem,
    updateItem,
    deleteItem,
    testItem,
    introspectDatasource,
    reindexKnowledgeBase,
    uploadKnowledgeFile,
    listKnowledgeFiles,
    deleteKnowledgeFile,
    reindexKnowledgeFile,
    replaceSkillPackage,
    validateSkill,
    pollJob,
    cancelJob,
  };
}
