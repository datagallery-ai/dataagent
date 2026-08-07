import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { createMetadataStore, createVerifiedTestIdentity } from "./index.js";

describe("RunRepository.findPreviousRunBySession", () => {
  it("returns the most recent terminal run in the session excluding the current run", () => {
    const root = mkdtempSync(join(tmpdir(), "run-previous-by-session-"));
    const metadata = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
    const { userId } = createVerifiedTestIdentity(metadata);
    try {
      metadata.sessions.create({ user_id: userId, id: "session-1", title: "Prev" });
      metadata.runs.create({
        user_id: userId, id: "run-1", session_id: "session-1",
        user_input: "帮我分析当前数据", status: "completed"
      });
      metadata.runs.create({
        user_id: userId, id: "run-2", session_id: "session-1",
        user_input: "再次尝试", status: "running"
      });

      const previous = metadata.runs.findPreviousRunBySession({
        user_id: userId, session_id: "session-1", exclude_run_id: "run-2"
      });

      expect(previous?.id).toBe("run-1");
      expect(previous?.user_input).toBe("帮我分析当前数据");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("returns undefined when the only run in the session is the current run", () => {
    const root = mkdtempSync(join(tmpdir(), "run-previous-none-"));
    const metadata = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
    const { userId } = createVerifiedTestIdentity(metadata);
    try {
      metadata.sessions.create({ user_id: userId, id: "session-1", title: "Solo" });
      metadata.runs.create({
        user_id: userId, id: "run-1", session_id: "session-1",
        user_input: "hello", status: "completed"
      });

      const previous = metadata.runs.findPreviousRunBySession({
        user_id: userId, session_id: "session-1", exclude_run_id: "run-1"
      });

      expect(previous).toBeUndefined();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("ignores non-terminal runs when selecting the previous run", () => {
    const root = mkdtempSync(join(tmpdir(), "run-previous-terminal-"));
    const metadata = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
    const { userId } = createVerifiedTestIdentity(metadata);
    try {
      metadata.sessions.create({ user_id: userId, id: "session-1", title: "Mixed" });
      // A suspended (non-terminal) run started earlier than the completed one.
      metadata.runs.create({
        user_id: userId, id: "run-1", session_id: "session-1",
        user_input: "suspended earlier", status: "suspended"
      });
      metadata.runs.create({
        user_id: userId, id: "run-2", session_id: "session-1",
        user_input: "completed later", status: "completed"
      });
      metadata.runs.create({
        user_id: userId, id: "run-3", session_id: "session-1",
        user_input: "current", status: "running"
      });

      const previous = metadata.runs.findPreviousRunBySession({
        user_id: userId, session_id: "session-1", exclude_run_id: "run-3"
      });

      // Should pick run-2 (completed), not run-1 (suspended, excluded by status filter).
      expect(previous?.id).toBe("run-2");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
