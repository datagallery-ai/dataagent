import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createMetadataStore, createVerifiedTestIdentity, type MetadataStore } from "@datafoundry/metadata";

import { commitSessionIntentFromRoute, resolveSessionIntentForRun } from "./session-intent.js";

describe("session intent run wiring", () => {
  let root: string;
  let metadata: MetadataStore;
  let userId: string;
  let nextRunNumber: number;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "session-intent-wiring-"));
    metadata = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
    userId = createVerifiedTestIdentity(metadata).userId;
    metadata.sessions.create({ user_id: userId, id: "session-1", title: "t" });
    nextRunNumber = 0;
  });

  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  const persist = (input: {
    source: string;
    reasonCodes?: string[];
    userInput?: string;
    protocolId?: string;
    taskRelation?: "continue" | "refine" | "replace" | "side-chat";
  }) => {
    nextRunNumber += 1;
    const runId = `run-${nextRunNumber}`;
    metadata.runs.create({
      user_id: userId,
      id: runId,
      session_id: "session-1",
      user_input: input.userInput ?? "帮我分析当前数据",
      status: "running"
    });
    const base = resolveSessionIntentForRun({ metadataStore: metadata, userId, sessionId: "session-1" });
    return commitSessionIntentFromRoute({
      metadataStore: metadata,
      userId,
      sessionId: "session-1",
      runId,
      userInput: input.userInput ?? "帮我分析当前数据",
      ...(base ? { expectedBaseRevisionId: base.revisionId } : {}),
      route: {
        definition: { id: input.protocolId ?? "data-analysis", version: "1" },
        reasonCodes: input.reasonCodes ?? [],
        source: input.source,
        taskRelation: input.taskRelation ?? "replace"
      }
    });
  };

  it.each([
    ["explicit", []],
    ["classifier", ["FOLLOW_UP"]],
    ["deterministic", ["ANALYTIC_INTENT"]]
  ])("persists the intent for a %s route", (source, reasonCodes) => {
    expect(persist({ source, reasonCodes: reasonCodes as string[] })).toBeDefined();
    expect(resolveSessionIntentForRun({ metadataStore: metadata, userId, sessionId: "session-1" }))
      .toMatchObject({ protocolId: "data-analysis", protocolVersion: "1", intentText: "帮我分析当前数据" });
  });

  it.each([
    ["default", []],
    ["deterministic", ["SESSION_INTENT_INHERITED"]],
    ["deterministic", ["PROTOCOL_SEGMENT_RESTORED"]]
  ])("does not overwrite the intent for a %s route with %j", (source, reasonCodes) => {
    expect(persist({ source: "deterministic", reasonCodes: ["ANALYTIC_INTENT"] })).toBeDefined();

    expect(persist({
      source,
      reasonCodes: reasonCodes as string[],
      userInput: "再次尝试",
      protocolId: "general-task",
      taskRelation: "side-chat"
    })).toBeDefined();

    expect(resolveSessionIntentForRun({ metadataStore: metadata, userId, sessionId: "session-1" }))
      .toMatchObject({ protocolId: "data-analysis", protocolVersion: "1", intentText: "帮我分析当前数据" });
  });

  it("skips persistence for empty user input", () => {
    expect(persist({ source: "classifier", userInput: "   " })).toBeUndefined();
    expect(resolveSessionIntentForRun({ metadataStore: metadata, userId, sessionId: "session-1" }))
      .toBeUndefined();
  });

  it("resolves the intent for a branched session through its lineage", () => {
    const active = persist({ source: "deterministic", reasonCodes: ["ANALYTIC_INTENT"] });
    expect(active).toBeDefined();
    metadata.sessions.create({ user_id: userId, id: "session-branch", title: "b" });
    metadata.sessionBranches.create({
      user_id: userId, id: "branch:session-branch", child_session_id: "session-branch",
      parent_session_id: "session-1", root_session_id: "session-1",
      fork_run_id: "run-1", fork_message_end_position: 1,
      fork_intent_revision_id: active!.revisionId
    });

    expect(resolveSessionIntentForRun({ metadataStore: metadata, userId, sessionId: "session-branch" }))
      .toMatchObject({ protocolId: "data-analysis", protocolVersion: "1", intentText: "帮我分析当前数据" });
  });

  it("fails route commit when the resolved base changed during agent assembly", () => {
    const original = persist({ source: "deterministic", reasonCodes: ["ANALYTIC_INTENT"] });
    metadata.runs.create({
      user_id: userId,
      id: "run-concurrent",
      session_id: "session-1",
      user_input: "写 README",
      status: "running"
    });
    const concurrent = metadata.sessionIntents.upsert({
      user_id: userId,
      session_id: "session-1",
      protocol_id: "general-task",
      protocol_version: "1",
      intent_text: "写 README",
      source_run_id: "run-concurrent"
    });
    metadata.runs.create({
      user_id: userId,
      id: "run-stale",
      session_id: "session-1",
      user_input: "继续分析",
      status: "running"
    });

    expect(() => commitSessionIntentFromRoute({
      metadataStore: metadata,
      userId,
      sessionId: "session-1",
      runId: "run-stale",
      userInput: "继续分析",
      expectedBaseRevisionId: original!.revisionId,
      route: {
        definition: { id: "data-analysis", version: "1" },
        reasonCodes: ["FOLLOW_UP"],
        source: "classifier",
        taskRelation: "continue"
      }
    })).toThrow("SESSION_INTENT_REVISION_CONFLICT:session-1");
    expect(resolveSessionIntentForRun({ metadataStore: metadata, userId, sessionId: "session-1" })?.revisionId)
      .toBe(concurrent.id);
  });

  it("resumes from the run's active binding even after the session head is replaced", () => {
    const original = persist({ source: "deterministic", reasonCodes: ["ANALYTIC_INTENT"] });
    const originalBinding = metadata.sessionIntents.findRunBinding({ user_id: userId, run_id: "run-1" });
    const replacement = persist({
      source: "classifier",
      userInput: "写 README",
      protocolId: "general-task",
      taskRelation: "replace"
    });

    expect(resolveSessionIntentForRun({
      metadataStore: metadata,
      userId,
      sessionId: "session-1",
      runId: "run-1"
    })?.revisionId).toBe(original?.revisionId);
    expect(resolveSessionIntentForRun({ metadataStore: metadata, userId, sessionId: "session-1" })?.revisionId)
      .toBe(replacement?.revisionId);

    const resumed = commitSessionIntentFromRoute({
      metadataStore: metadata,
      userId,
      sessionId: "session-1",
      runId: "run-1",
      userInput: "再次尝试",
      expectedBaseRevisionId: original!.revisionId,
      route: {
        definition: { id: "data-analysis", version: "1" },
        reasonCodes: ["PROTOCOL_SEGMENT_RESTORED"],
        source: "deterministic",
        taskRelation: "continue"
      }
    });

    expect(resumed?.revisionId).toBe(original?.revisionId);
    expect(metadata.sessionIntents.findRunBinding({ user_id: userId, run_id: "run-1" }))
      .toEqual(originalBinding);
    expect(resolveSessionIntentForRun({ metadataStore: metadata, userId, sessionId: "session-1" })?.revisionId)
      .toBe(replacement?.revisionId);
  });

  it("does not give a previously intent-less run a future session task on resume", () => {
    const sideChat = persist({
      source: "default",
      userInput: "谢谢",
      protocolId: "general-task",
      taskRelation: "side-chat"
    });
    expect(sideChat).toBeUndefined();
    const replacement = persist({
      source: "classifier",
      userInput: "分析订单",
      protocolId: "data-analysis",
      taskRelation: "replace"
    });

    expect(resolveSessionIntentForRun({
      metadataStore: metadata,
      userId,
      sessionId: "session-1",
      runId: "run-1"
    })).toBeUndefined();
    expect(commitSessionIntentFromRoute({
      metadataStore: metadata,
      userId,
      sessionId: "session-1",
      runId: "run-1",
      userInput: "谢谢",
      route: {
        definition: { id: "general-task", version: "1" },
        reasonCodes: ["PROTOCOL_SEGMENT_RESTORED"],
        source: "deterministic",
        taskRelation: "continue"
      }
    })).toBeUndefined();
    expect(resolveSessionIntentForRun({ metadataStore: metadata, userId, sessionId: "session-1" })?.revisionId)
      .toBe(replacement?.revisionId);
  });
});
