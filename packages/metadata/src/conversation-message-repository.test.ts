import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { createMetadataStore, createVerifiedTestIdentity } from "./index.js";

describe("ConversationMessageRepository", () => {
  it("finds only the latest persisted assistant message for the requested run", () => {
    const root = mkdtempSync(join(tmpdir(), "conversation-message-"));
    try {
      const metadata = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
      const { userId } = createVerifiedTestIdentity(metadata);
      metadata.sessions.create({ user_id: userId, id: "session-1", title: "Conversation" });
      for (const runId of ["run-1", "run-2"]) {
        metadata.runs.create({
          user_id: userId,
          id: runId,
          session_id: "session-1",
          user_input: "test",
          status: "running"
        });
      }
      const append = (runId: string, role: "assistant" | "user", messageId: string): void => {
        metadata.conversationMessages.append({
          id: `${runId}:${messageId}`,
          user_id: userId,
          session_id: "session-1",
          run_id: runId,
          role,
          source: role === "assistant" ? "agent" : "client",
          message_id: messageId,
          content_text: messageId
        });
      };
      append("run-1", "assistant", "assistant-1");
      append("run-2", "assistant", "assistant-other-run");
      append("run-1", "user", "user-latest");
      append("run-1", "assistant", "assistant-2");

      expect(metadata.conversationMessages.findLatestAssistantByRun({
        user_id: userId,
        session_id: "session-1",
        run_id: "run-1"
      })?.message_id).toBe("assistant-2");
      metadata.close();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
