import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createMetadataStore, createVerifiedTestIdentity, type MetadataStore } from "@datafoundry/metadata";

import { persistSessionIntentFromRoute, resolveSessionIntentForRun } from "./session-intent.js";

describe("session intent run wiring", () => {
  let root: string;
  let metadata: MetadataStore;
  let userId: string;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "session-intent-wiring-"));
    metadata = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
    userId = createVerifiedTestIdentity(metadata).userId;
    metadata.sessions.create({ user_id: userId, id: "session-1", title: "t" });
    metadata.runs.create({
      user_id: userId, id: "run-1", session_id: "session-1", user_input: "帮我分析当前数据", status: "running"
    });
  });

  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  const persist = (input: {
    source: string;
    reasonCodes?: string[];
    userInput?: string;
    protocolId?: string;
  }): boolean => persistSessionIntentFromRoute({
    metadataStore: metadata,
    userId,
    sessionId: "session-1",
    runId: "run-1",
    userInput: input.userInput ?? "帮我分析当前数据",
    route: {
      definition: { id: input.protocolId ?? "data-analysis", version: "1" },
      reasonCodes: input.reasonCodes ?? [],
      source: input.source
    }
  });

  it.each([
    ["explicit", []],
    ["classifier", ["FOLLOW_UP"]],
    ["deterministic", ["ANALYTIC_INTENT"]]
  ])("persists the intent for a %s route", (source, reasonCodes) => {
    expect(persist({ source, reasonCodes: reasonCodes as string[] })).toBe(true);
    expect(resolveSessionIntentForRun({ metadataStore: metadata, userId, sessionId: "session-1" }))
      .toEqual({ protocolId: "data-analysis", protocolVersion: "1", intentText: "帮我分析当前数据" });
  });

  it.each([
    ["default", []],
    ["deterministic", ["SESSION_INTENT_INHERITED"]],
    ["deterministic", ["PROTOCOL_SEGMENT_RESTORED"]]
  ])("does not overwrite the intent for a %s route with %j", (source, reasonCodes) => {
    expect(persist({ source: "deterministic", reasonCodes: ["ANALYTIC_INTENT"] })).toBe(true);

    expect(persist({
      source,
      reasonCodes: reasonCodes as string[],
      userInput: "再次尝试",
      protocolId: "general-task"
    })).toBe(false);

    expect(resolveSessionIntentForRun({ metadataStore: metadata, userId, sessionId: "session-1" }))
      .toEqual({ protocolId: "data-analysis", protocolVersion: "1", intentText: "帮我分析当前数据" });
  });

  it("skips persistence for empty user input", () => {
    expect(persist({ source: "classifier", userInput: "   " })).toBe(false);
    expect(resolveSessionIntentForRun({ metadataStore: metadata, userId, sessionId: "session-1" }))
      .toBeUndefined();
  });

  it("resolves the intent for a branched session through its lineage", () => {
    expect(persist({ source: "deterministic", reasonCodes: ["ANALYTIC_INTENT"] })).toBe(true);
    metadata.sessions.create({ user_id: userId, id: "session-branch", title: "b" });
    metadata.sessionBranches.create({
      user_id: userId, id: "branch:session-branch", child_session_id: "session-branch",
      parent_session_id: "session-1", root_session_id: "session-1",
      fork_run_id: "run-1", fork_message_end_position: 1
    });

    expect(resolveSessionIntentForRun({ metadataStore: metadata, userId, sessionId: "session-branch" }))
      .toEqual({ protocolId: "data-analysis", protocolVersion: "1", intentText: "帮我分析当前数据" });
  });
});
