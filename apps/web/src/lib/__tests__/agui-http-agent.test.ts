import { describe, expect, it } from "vitest";
import { HttpAgent } from "@ag-ui/client";

import { createDataFoundryHttpAgent, syncHttpAgentHeaders } from "../agui-http-agent";

describe("createDataFoundryHttpAgent", () => {
  it("points at the same-origin AG-UI endpoint with current CSRF headers", () => {
    const agent = createDataFoundryHttpAgent(
      { "X-CSRF-Token": "csrf-1" },
      { agentId: "dataFoundry", threadId: "thread-1" },
    );
    expect(agent).toBeInstanceOf(HttpAgent);
    expect(agent.url).toBe("/api/copilotkit");
    expect(agent.headers["X-CSRF-Token"]).toBe("csrf-1");
    expect(agent.agentId).toBe("dataFoundry");
    expect(agent.threadId).toBe("thread-1");
  });

  it("updates headers on the existing agent instance", () => {
    const agent = createDataFoundryHttpAgent({ "X-CSRF-Token": "csrf-1" });
    syncHttpAgentHeaders(agent, { "X-CSRF-Token": "csrf-2" });
    expect(agent.headers["X-CSRF-Token"]).toBe("csrf-2");
  });
});
