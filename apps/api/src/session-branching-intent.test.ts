import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createMetadataStore, createVerifiedTestIdentity, type MetadataStore } from "@datafoundry/metadata";

import { createSessionBranch } from "./session-branching.js";

describe("session branch intent pinning", () => {
  let root: string;
  let metadata: MetadataStore;
  let userId: string;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "session-branch-intent-"));
    metadata = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
    userId = createVerifiedTestIdentity(metadata).userId;
    metadata.sessions.create({ user_id: userId, id: "parent", title: "parent" });
    metadata.runs.create({
      user_id: userId, id: "run-a", session_id: "parent", user_input: "分析订单", status: "completed"
    });
    metadata.runs.create({
      user_id: userId, id: "run-b", session_id: "parent", user_input: "分析退货", status: "completed"
    });
  });

  afterEach(() => rmSync(root, { recursive: true, force: true }));

  const seedRevisions = () => {
    const first = metadata.sessionIntents.upsert({
      user_id: userId, session_id: "parent", protocol_id: "data-analysis",
      protocol_version: "1", intent_text: "分析订单", source_run_id: "run-a"
    });
    const second = metadata.sessionIntents.upsert({
      user_id: userId, session_id: "parent", protocol_id: "data-analysis",
      protocol_version: "1", intent_text: "分析退货", source_run_id: "run-b"
    });
    metadata.sessionIntents.bindRun({
      user_id: userId,
      run_id: "run-b",
      session_id: "parent",
      base_revision_id: first.id,
      active_revision_id: second.id,
      task_relation: "replace"
    });
    return { first, second };
  };

  it("uses the target run base revision for a run rewrite branch", () => {
    const { first } = seedRevisions();
    const created = createSessionBranch({
      activeSessionId: "parent", metadataStore: metadata, runId: "run-b", userId
    });

    expect(created.branch.fork_intent_revision_id).toBe(first.id);
    expect(metadata.sessionIntents.resolveForSession({
      user_id: userId, session_id: created.session.id
    })?.id).toBe(first.id);
  });

  it("uses the checkpoint active revision for a checkpoint branch", () => {
    const { second } = seedRevisions();
    const snapshot = metadata.contextPackageSnapshots.create({
      user_id: userId,
      session_id: "parent",
      run_id: "run-b",
      package_id: "context-b",
      revision: 0,
      payload: {}
    });
    metadata.checkpoints.create({
      id: "checkpoint-b",
      user_id: userId,
      session_id: "parent",
      run_id: "run-b",
      event_seq: 1,
      context_package_id: snapshot.id,
      context_package_revision: 0,
      kind: "protocol-phase",
      status: "stable",
      label: "after handoff",
      intent_revision_id: second.id
    });
    const created = createSessionBranch({
      activeSessionId: "parent", checkpointId: "checkpoint-b", metadataStore: metadata, userId
    });

    expect(created.branch.fork_intent_revision_id).toBe(second.id);
  });
});
