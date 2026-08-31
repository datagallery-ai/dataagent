import { normalizeIngressMessages } from "../packages/agent-runtime/dist/testing.js";

const messages = normalizeIngressMessages([
  {
    id: "activity",
    role: "activity",
    content: "hidden"
  },
  {
    id: "reasoning",
    role: "reasoning",
    content: "hidden"
  },
  {
    id: "user-upload",
    role: "user",
    content: [
      { type: "text", text: "分析这个 CSV" },
      {
        type: "document",
        source: { type: "url", value: "uploads/orders.csv", mimeType: "text/csv" },
        metadata: { filename: "orders.csv" }
      },
      {
        type: "document",
        source: { type: "url", value: "https://example.com/remote.csv", mimeType: "text/csv" }
      },
      {
        type: "document",
        source: { type: "url", value: "uploads/../secret.csv", mimeType: "text/csv" }
      }
    ]
  }
]);

assert(messages.length === 1, "activity/reasoning ingress messages should be filtered");
const user = messages[0];
assert(user.role === "user", "user message should be preserved");
assert(Array.isArray(user.content), "user content parts should remain an array");

const text = user.content
  .filter((part) => part.type === "text")
  .map((part) => part.text)
  .join("\n");
assert(text.includes("Uploaded workspace file:"), "workspace upload should be projected into model-visible text");
assert(text.includes("path: uploads/orders.csv"), "workspace upload projection should include the read_file path");
assert(text.includes("Use the workspace read_file tool"), "projection should instruct the model how to consume it");
assert(!text.includes("https://example.com/remote.csv"), "remote URLs should not be projected as workspace files");
assert(!text.includes("uploads/../secret.csv"), "unsafe upload paths should not be projected");

console.log("Ingress message normalization tests OK");
process.exit(0);

function assert(condition, message) {
  if (!condition) {
    console.error(`ASSERT FAILED: ${message}`);
    process.exit(1);
  }
}
