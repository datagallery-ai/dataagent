import type { HttpAgent } from "@ag-ui/client";
import { StreamHttpAgent } from "./stream-http-agent";

export const runtimeAgentId = "dataFoundry";
const sessionAgentIdPrefix = `${runtimeAgentId}:session:`;

export function dataTaskSessionAgentId(threadId: string): string {
  return `${sessionAgentIdPrefix}${encodeURIComponent(threadId)}`;
}

export function threadIdFromDataTaskSessionAgentId(agentId: unknown): string | null {
  if (typeof agentId !== "string" || !agentId.startsWith(sessionAgentIdPrefix)) {
    return null;
  }
  try {
    return decodeURIComponent(agentId.slice(sessionAgentIdPrefix.length));
  } catch {
    return null;
  }
}

export function createSessionHttpAgent(threadId: string): HttpAgent {
  return new StreamHttpAgent({
    agentId: dataTaskSessionAgentId(threadId),
    threadId,
  });
}
