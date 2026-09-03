import { HttpAgent } from "@ag-ui/client";

import { getAgentRuntimeUrl } from "./config-api";

export function createDataFoundryHttpAgent(
  headers: Record<string, string>,
  options?: { agentId?: string; threadId?: string },
): HttpAgent {
  return new HttpAgent({
    url: getAgentRuntimeUrl(),
    headers,
    agentId: options?.agentId,
    threadId: options?.threadId,
  });
}

export function syncHttpAgentHeaders(agent: HttpAgent, headers: Record<string, string>): void {
  agent.headers = { ...headers };
}
