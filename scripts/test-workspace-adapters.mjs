/**
 * Contract tests for workspace tool context adapters (Mastra 1.42 string returns).
 */
import {
  WriteFileToolObservationAdapter,
  ReadFileToolObservationAdapter,
  ExecuteCommandToolObservationAdapter,
  projectWorkspaceObservation
} from "../packages/agent-runtime/dist/context/tool-observation/adapters/workspace-tool-observation-adapters.js";

const budget = { maxChars: 12000 };

const writeAdapter = new WriteFileToolObservationAdapter();
const writeItems = writeAdapter.toContextItems("Wrote 28 bytes to verify-test.txt", budget);
const writeModel = writeItems.find((item) => item.visibility === "model")?.content;
const expectedWriteModel = "Wrote 28 bytes to verify-test.txt";
assert(
  writeModel === expectedWriteModel,
  `write_file model content must preserve string observation, got ${JSON.stringify(writeModel)}`
);
assert(typeof writeModel === "string" && writeModel.length > 0, "write_file must not project to empty string");

const readAdapter = new ReadFileToolObservationAdapter();
const readItems = readAdapter.toContextItems("1| hello\n", budget);
const readModel = readItems.find((item) => item.visibility === "model")?.content;
assert(readModel === "1| hello\n", "read_file must preserve stdout string");

const execAdapter = new ExecuteCommandToolObservationAdapter();
const execItems = execAdapter.toContextItems("verify-ok\n", budget);
const execModel = execItems.find((item) => item.visibility === "model")?.content;
assert(execModel === "verify-ok\n", "execute_command must preserve stdout string");

const legacy = projectWorkspaceObservation({ path: "/tmp/a.txt", success: true });
assert(typeof legacy === "string" && legacy.includes("path"), "legacy object must stringify");

console.log("Workspace adapter contract tests OK");
process.exit(0);

function assert(condition, message) {
  if (!condition) {
    console.error(`ASSERT FAILED: ${message}`);
    process.exit(1);
  }
}
