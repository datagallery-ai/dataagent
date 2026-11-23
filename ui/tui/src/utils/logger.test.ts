import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { it } from "node:test";
import { initLogger } from "./logger.js";

it("records RUN_ERROR metadata and the original transport exception", () => {
  const directory = mkdtempSync(path.join(tmpdir(), "tui-logging-"));
  try {
    const logger = initLogger({ logDir: directory });
    logger.error("Run failed", { threadId: "thread-1", runId: "run-1", code: "MODEL_ERROR", message: "upstream failure" });
    logger.error("Stream failed", new Error("Bearer synthetic-token"), { threadId: "thread-1", runId: "run-2" });
    const text = readFileSync(logger.getLogFilePath(), "utf8");
    const records = text.trim().split("\n").map(line => JSON.parse(line));
    assert.equal(records[0].threadId, "thread-1");
    assert.equal(records[0].runId, "run-1");
    assert.equal(records[0].code, "MODEL_ERROR");
    assert.equal(records[0].errorMessage, "upstream failure");
    assert.match(records[1].error.stack, /logger.test/);
    assert(!text.includes("synthetic-token"));
  } finally { rmSync(directory, { recursive: true, force: true }); }
});
