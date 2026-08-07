import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createMetadataStore, createVerifiedTestIdentity, type MetadataStore } from "./index.js";

describe("SessionIntentRepository", () => {
  let root: string;
  let metadata: MetadataStore;
  let userId: string;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "session-intent-"));
    metadata = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
    userId = createVerifiedTestIdentity(metadata).userId;
  });

  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  const seedSessionWithRun = (sessionId: string, runId: string, userInput: string): void => {
    metadata.sessions.create({ user_id: userId, id: sessionId, title: "t" });
    metadata.runs.create({
      user_id: userId,
      id: runId,
      session_id: sessionId,
      user_input: userInput,
      status: "completed"
    });
  };

  it("upserts and reads the session intent", () => {
    seedSessionWithRun("session-1", "run-1", "帮我分析当前数据");

    metadata.sessionIntents.upsert({
      user_id: userId,
      session_id: "session-1",
      protocol_id: "data-analysis",
      protocol_version: "1",
      intent_text: "帮我分析当前数据",
      source_run_id: "run-1"
    });
    const intent = metadata.sessionIntents.find({ user_id: userId, session_id: "session-1" });

    expect(intent).toMatchObject({
      protocol_id: "data-analysis",
      protocol_version: "1",
      intent_text: "帮我分析当前数据",
      source_run_id: "run-1"
    });
  });

  it("overwrites the previous intent on upsert", () => {
    seedSessionWithRun("session-1", "run-1", "first");
    metadata.runs.create({
      user_id: userId, id: "run-2", session_id: "session-1", user_input: "second", status: "completed"
    });

    metadata.sessionIntents.upsert({
      user_id: userId, session_id: "session-1", protocol_id: "general-task",
      protocol_version: "1", intent_text: "first", source_run_id: "run-1"
    });
    metadata.sessionIntents.upsert({
      user_id: userId, session_id: "session-1", protocol_id: "data-analysis",
      protocol_version: "1", intent_text: "second", source_run_id: "run-2"
    });

    expect(metadata.sessionIntents.find({ user_id: userId, session_id: "session-1" })).toMatchObject({
      protocol_id: "data-analysis",
      intent_text: "second",
      source_run_id: "run-2"
    });
  });

  it("returns undefined for a session without intent or lineage", () => {
    seedSessionWithRun("session-1", "run-1", "hello");

    expect(metadata.sessionIntents.resolveForSession({ user_id: userId, session_id: "session-1" }))
      .toBeUndefined();
  });

  it("resolves a branched session's intent through its parent lineage", () => {
    seedSessionWithRun("session-root", "run-root", "帮我分析当前数据");
    metadata.sessionIntents.upsert({
      user_id: userId, session_id: "session-root", protocol_id: "data-analysis",
      protocol_version: "1", intent_text: "帮我分析当前数据", source_run_id: "run-root"
    });
    // First-level branch, then a branch of the branch: neither has its own intent.
    metadata.sessions.create({ user_id: userId, id: "session-branch", title: "b" });
    metadata.sessionBranches.create({
      user_id: userId, id: "branch:session-branch", child_session_id: "session-branch",
      parent_session_id: "session-root", root_session_id: "session-root",
      fork_run_id: "run-root", fork_message_end_position: 1
    });
    metadata.sessions.create({ user_id: userId, id: "session-grandchild", title: "g" });
    metadata.sessionBranches.create({
      user_id: userId, id: "branch:session-grandchild", child_session_id: "session-grandchild",
      parent_session_id: "session-branch", root_session_id: "session-root",
      fork_run_id: "run-root", fork_message_end_position: 1
    });

    const resolved = metadata.sessionIntents.resolveForSession({
      user_id: userId,
      session_id: "session-grandchild"
    });

    expect(resolved).toMatchObject({
      session_id: "session-root",
      protocol_id: "data-analysis",
      intent_text: "帮我分析当前数据"
    });
  });

  it("prefers the branched session's own intent over its parent's", () => {
    seedSessionWithRun("session-root", "run-root", "分析订单");
    metadata.sessionIntents.upsert({
      user_id: userId, session_id: "session-root", protocol_id: "data-analysis",
      protocol_version: "1", intent_text: "分析订单", source_run_id: "run-root"
    });
    metadata.sessions.create({ user_id: userId, id: "session-branch", title: "b" });
    metadata.sessionBranches.create({
      user_id: userId, id: "branch:session-branch", child_session_id: "session-branch",
      parent_session_id: "session-root", root_session_id: "session-root",
      fork_run_id: "run-root", fork_message_end_position: 1
    });
    metadata.runs.create({
      user_id: userId, id: "run-branch", session_id: "session-branch",
      user_input: "分析退货", status: "completed"
    });
    metadata.sessionIntents.upsert({
      user_id: userId, session_id: "session-branch", protocol_id: "data-analysis",
      protocol_version: "1", intent_text: "分析退货", source_run_id: "run-branch"
    });

    expect(metadata.sessionIntents.resolveForSession({ user_id: userId, session_id: "session-branch" }))
      .toMatchObject({ session_id: "session-branch", intent_text: "分析退货" });
  });

  it("deletes session intents with the session", () => {
    seedSessionWithRun("session-1", "run-1", "分析");
    metadata.sessionIntents.upsert({
      user_id: userId, session_id: "session-1", protocol_id: "data-analysis",
      protocol_version: "1", intent_text: "分析", source_run_id: "run-1"
    });

    const result = metadata.sessions.delete({ user_id: userId, session_id: "session-1" });

    expect(result.deleted).toBe(true);
    expect(metadata.sessionIntents.find({ user_id: userId, session_id: "session-1" })).toBeUndefined();
  });
});
