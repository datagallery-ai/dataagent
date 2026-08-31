import { LocalFileAssetService } from "@datafoundry/files";
import { createMetadataStore, createVerifiedTestIdentity } from "@datafoundry/metadata";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { SessionOutputService } from "./session-output-service.js";

const roots: string[] = [];

const createTestServices = () => {
  const root = mkdtempSync(join(tmpdir(), "session-output-service-"));
  roots.push(root);
  const metadataStore = createMetadataStore({
    database_path: join(root, "metadata.sqlite")
  });
  const { userId, workspaceId } = createVerifiedTestIdentity(metadataStore, {
    email: "user@example.com",
    displayName: "Test User",
    workspaceName: "Workspace"
  });
  // Keep stable ids expected by assertions below.
  const sessionId = "session-1";
  const runId = "run-1";
  metadataStore.sessions.create({
    user_id: userId,
    id: sessionId
  });
  metadataStore.runs.create({
    user_id: userId,
    session_id: sessionId,
    id: runId,
    user_input: "test"
  });
  const fileAssetService = new LocalFileAssetService(metadataStore, {
    storageRoot: join(root, "files")
  });
  const sessionOutputService = new SessionOutputService(metadataStore, fileAssetService);
  return {
    fileAssetService,
    metadataStore,
    root,
    sessionOutputService,
    userId,
    workspaceId,
    sessionId,
    runId
  };
};

afterEach(() => {
  for (const root of roots.splice(0)) {
    rmSync(root, { force: true, recursive: true });
  }
});

describe("SessionOutputService", () => {
  it("returns null for paths excluded from session outputs", async () => {
    const { root, sessionOutputService, userId, workspaceId, sessionId, runId } = createTestServices();
    const sourcePath = join(root, "analysis.py");
    writeFileSync(sourcePath, "print('draft')\n");

    await expect(sessionOutputService.upsertFromSessionFile({
      user_id: userId,
      workspace_id: workspaceId,
      session_id: sessionId,
      run_id: runId,
      path: "analysis.py",
      source_path: sourcePath
    })).resolves.toBeNull();
  });

  it("upserts one output per session file path and appends versions", async () => {
    const { metadataStore, root, sessionOutputService, userId, workspaceId, sessionId, runId } =
      createTestServices();
    const sourcePath = join(root, "summary.md");
    writeFileSync(sourcePath, "# First\n");

    const first = await sessionOutputService.upsertFromSessionFile({
      user_id: userId,
      workspace_id: workspaceId,
      session_id: sessionId,
      run_id: runId,
      path: "reports/summary.md",
      source_path: sourcePath,
      tool_call_id: "tool-1"
    });

    writeFileSync(sourcePath, "# Second\n");
    const second = await sessionOutputService.upsertFromSessionFile({
      user_id: userId,
      workspace_id: workspaceId,
      session_id: sessionId,
      run_id: runId,
      path: "reports/summary.md",
      source_path: sourcePath,
      tool_call_id: "tool-2"
    });

    expect(first).not.toBeNull();
    expect(second).not.toBeNull();
    expect(second?.artifact.id).toBe(first?.artifact.id);
    expect(second?.artifact.file_asset_ref_id).not.toBe(first?.artifact.file_asset_ref_id);
    expect(metadataStore.artifacts.listBySession({
      user_id: userId,
      session_id: sessionId
    })).toHaveLength(1);
    expect(metadataStore.artifacts.findBySessionLogicalKey({
      user_id: userId,
      session_id: sessionId,
      logical_key: "session_file:reports/summary.md"
    })?.id).toBe(first?.artifact.id);

    const versions = metadataStore.artifactVersions.listByArtifact({
      user_id: userId,
      artifact_id: first?.artifact.id ?? ""
    });
    expect(versions.map((version) => version.version)).toEqual([1, 2]);
    expect(versions[0]?.file_asset_ref_id).toBe(first?.version.file_asset_ref_id);
    expect(versions[1]?.file_asset_ref_id).toBe(second?.version.file_asset_ref_id);
  });
});
