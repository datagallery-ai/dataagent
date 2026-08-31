/**
 * Provision the DTC Growth Review SQLite case library for each user+workspace.
 *
 * Copies the repo fixture into the workspace datasources directory (user-writable
 * copy) and registers a sqlite datasource pointing at that real file path.
 * Idempotent: skips when already registered with a readable path; repairs a
 * missing file / broken path; does not revive user-deleted datasources.
 */
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { resolveWorkspaceDir } from "@datafoundry/agent-runtime";
import type { MetadataStore } from "@datafoundry/metadata";

export const DTC_GROWTH_DATASOURCE_ID = "dtc-growth-demo";
export const DTC_GROWTH_DATASOURCE_NAME = "DTC Growth Review";
export const DTC_GROWTH_FIXTURE_FILENAME = "dtc-growth-demo.sqlite";

const MODULE_DIR = dirname(fileURLToPath(import.meta.url));
const DEFAULT_QUERY_POLICY = {
  maxRows: 1000,
  timeoutMs: 10000,
  denyWrite: true
} as const;

/** Resolve the canonical fixture shipped with the repo (or env override). */
export const resolveDtcGrowthFixturePath = (injectedModuleDir = MODULE_DIR): string => {
  const fromEnv = process.env.DTC_GROWTH_FIXTURE_PATH?.trim();
  if (fromEnv) {
    return resolve(fromEnv);
  }
  // apps/api/src|dist → repo root storage/fixtures/...
  return resolve(injectedModuleDir, "../../../storage/fixtures", DTC_GROWTH_FIXTURE_FILENAME);
};

export type EnsureBuiltinDtcGrowthDatasourceInput = {
  metadataStore: MetadataStore;
  userId: string;
  workspaceId: string;
  /** Override for tests. */
  fixturePath?: string;
  workspaceRoot?: string;
};

export type EnsureBuiltinDtcGrowthDatasourceResult = {
  action: "created" | "repaired" | "skipped" | "fixture_missing";
  filePath?: string;
};

/**
 * Ensure the current user has a usable DTC Growth Review sqlite datasource.
 * Safe to call repeatedly; intended to run from builtin config bootstrap.
 */
export const ensureBuiltinDtcGrowthDatasource = (
  input: EnsureBuiltinDtcGrowthDatasourceInput
): EnsureBuiltinDtcGrowthDatasourceResult => {
  const fixturePath = input.fixturePath ?? resolveDtcGrowthFixturePath();
  if (!existsSync(fixturePath)) {
    console.warn(`[builtin] DTC Growth fixture missing at ${fixturePath}; skip auto-provision`);
    return { action: "fixture_missing" };
  }

  const workspaceRoot =
    input.workspaceRoot
    ?? process.env.WORKSPACE_ROOT
    ?? join(process.env.STORAGE_ROOT_DIR ?? "storage", "workspaces");
  const workspaceDir = resolveWorkspaceDir({
    runContext: {
      user_id: input.userId,
      workspace_id: input.workspaceId,
      session_id: "builtin-dtc-growth",
      run_id: "builtin-dtc-growth",
      selected_datasource_id: "",
      enabled_datasource_ids: [],
      user_input: "",
      chat_mode: "server",
      model_name: "builtin-dtc-growth"
    },
    workspaceRoot
  });
  const targetDir = join(workspaceDir, "datasources");
  const targetPath = join(targetDir, DTC_GROWTH_FIXTURE_FILENAME);
  if (!targetPath.startsWith(`${workspaceDir}${sep}`)) {
    throw new Error("WORKSPACE_PATH_ESCAPE");
  }

  const existing = input.metadataStore.dataSources.find({
    user_id: input.userId,
    datasource_id: DTC_GROWTH_DATASOURCE_ID
  });
  if (existing?.status === "deleted") {
    return { action: "skipped", filePath: targetPath };
  }

  const existingConfig = existing ? parseConfig(existing.config_json) : {};
  const existingPath = stringValue(existingConfig.path);
  const existingPathOk = Boolean(existingPath && existsSync(existingPath));

  // Prefer an already-valid registered path (e.g. another workspace copy) over rewriting.
  if (existing && existingPathOk && existingPath) {
    return { action: "skipped", filePath: existingPath };
  }

  if (!existsSync(targetPath)) {
    mkdirSync(targetDir, { recursive: true });
    copyFileSync(fixturePath, targetPath);
  }

  const config = {
    ...existingConfig,
    path: targetPath,
    builtin: true,
    defaultEnabled: true,
    mode: "readonly",
    queryPolicy: {
      ...DEFAULT_QUERY_POLICY,
      ...recordValue(existingConfig.queryPolicy)
    }
  };

  if (!existing) {
    input.metadataStore.dataSources.create({
      user_id: input.userId,
      id: DTC_GROWTH_DATASOURCE_ID,
      name: DTC_GROWTH_DATASOURCE_NAME,
      type: "sqlite",
      config,
      description: "Built-in beauty DTC growth review case (SQLite).",
      status: "ready"
    });
    return { action: "created", filePath: targetPath };
  }

  input.metadataStore.dataSources.create({
    user_id: input.userId,
    id: DTC_GROWTH_DATASOURCE_ID,
    name: existing.name || DTC_GROWTH_DATASOURCE_NAME,
    type: "sqlite",
    config,
    ...(existing.description ? { description: existing.description } : {
      description: "Built-in beauty DTC growth review case (SQLite)."
    }),
    status: existing.status === "failed" ? "ready" : existing.status,
    expected_revision: existing.revision
  });
  return { action: "repaired", filePath: targetPath };
};

const parseConfig = (configJson: string): Record<string, unknown> => {
  try {
    const parsed: unknown = JSON.parse(configJson);
    return isRecord(parsed) ? parsed : {};
  } catch {
    return {};
  }
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const stringValue = (value: unknown): string | undefined =>
  typeof value === "string" && value.trim() ? value.trim() : undefined;

const recordValue = (value: unknown): Record<string, unknown> | undefined =>
  isRecord(value) ? value : undefined;
