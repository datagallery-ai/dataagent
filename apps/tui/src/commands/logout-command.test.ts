import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { logoutCommand } from "./builtinCommands.js";
import type { CommandContext } from "./types.js";

describe("logoutCommand", () => {
  it("returns a logout action without touching storage itself", async () => {
    const result = await logoutCommand.execute([], {
      client: {},
      workspaceConfig: { db: [], llm: [], skill: [], mcp: [], knowledge: [], kb: [] },
      state: { messages: [] },
    } as unknown as CommandContext);

    assert.equal(result.success, true);
    assert.deepEqual(result.data, { action: "logout" });
    assert.match(result.message, /logging out/i);
  });
});
