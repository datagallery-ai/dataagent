// Example usage of CopilotKit client
import { CopilotKitClient, CopilotKitClientError } from "./copilotkit-client.js";
import type { RunAgentInput } from "./types.js";

/**
 * Example: Run agent and process streaming events
 */
async function exampleRunAgent() {
  const client = new CopilotKitClient({
    runtimeUrl: "http://127.0.0.1:8787/api/copilotkit",
    agent: "dataFoundry",
  });

  const input: RunAgentInput = {
    threadId: "thread-123",
    runId: "run-456",
    messages: [
      {
        id: "msg-123",
        role: "user",
        content: "Show me the sales data",
      },
    ],
    context: [
      {
        description: "datasource_id",
        value: "api-duckdb-demo",
      },
    ],
    tools: [],
    state: {
      datasourceId: "api-duckdb-demo",
    },
    forwardedProps: {
      datasourceId: "api-duckdb-demo",
    },
  };

  try {
    console.log("Starting agent run...");

    for await (const event of client.runAgent(input)) {
      switch (event.type) {
        case "RUN_STARTED":
          console.log("Run started");
          break;

        case "TEXT_MESSAGE_CONTENT":
          process.stdout.write(String((event as { delta?: unknown }).delta ?? ""));
          break;

        case "TEXT_MESSAGE_END":
          console.log("\nMessage complete");
          break;

        case "TOOL_CALL_START":
          console.log(
            `Tool call started: ${String((event as { toolCallName?: unknown }).toolCallName ?? "tool")}`,
          );
          break;

        case "TOOL_CALL_RESULT":
          console.log(`Tool result: ${String((event as { content?: unknown }).content ?? "")}`);
          break;

        case "RUN_FINISHED":
          console.log("Run completed");
          break;

        default:
          console.log("Event:", event.type);
      }
    }
  } catch (error) {
    if (error instanceof CopilotKitClientError) {
      console.error(`Error [${error.code}]:`, error.message);
      if (error.code === "PROVIDER_CONFIG_MISSING") {
        console.error("Please set LLM_API_KEY environment variable");
      }
    } else {
      console.error("Unexpected error:", error);
    }
  }
}

// Run example if executed directly
if (import.meta.url === `file://${process.argv[1]}`) {
  exampleRunAgent().catch(console.error);
}
