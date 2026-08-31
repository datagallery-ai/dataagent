import { toStandardSchema, type PublicSchema } from "@mastra/core/schema";
import { createTool, type ToolAction } from "@mastra/core/tools";

import {
  callPolicyMcpTool,
  type PolicyMcpClientConfig,
  type PolicyMcpToolConfig
} from "./policy-mcp-middleware.js";

export type PolicyMcpMastraTool = ToolAction<Record<string, unknown>, string, unknown, unknown>;
export type PolicyMcpMastraTools = Record<string, PolicyMcpMastraTool>;

/** Create server-side Mastra tools for the enabled MCP tool manifests. */
export const createPolicyMcpTools = (servers: PolicyMcpClientConfig[]): PolicyMcpMastraTools => {
  const usedNames = new Set<string>();
  const tools: PolicyMcpMastraTools = {};
  for (const server of servers) {
    for (const manifestTool of server.tools ?? []) {
      if (usedNames.has(manifestTool.name)) {
        throw new Error(`MCP_TOOL_NAME_CONFLICT:${server.serverId}:${manifestTool.name}`);
      }
      usedNames.add(manifestTool.name);
      tools[manifestTool.name] = createPolicyMcpTool(server, manifestTool);
    }
  }
  return tools;
};

const createPolicyMcpTool = (
  server: PolicyMcpClientConfig,
  manifestTool: PolicyMcpToolConfig
): PolicyMcpMastraTool =>
  createTool({
    id: manifestTool.name,
    description: manifestTool.description ?? `Call MCP tool ${manifestTool.name}.`,
    inputSchema: normalizeMcpInputSchema(manifestTool.inputSchema),
    execute: async (input) => callPolicyMcpTool(server, manifestTool.name, recordInput(input))
  }) as PolicyMcpMastraTool;

const normalizeMcpInputSchema = (schema: unknown): PublicSchema<Record<string, unknown>> =>
  toStandardSchema((isRecord(schema) ? schema : { type: "object", properties: {} }) as PublicSchema<
    Record<string, unknown>
  >);

const recordInput = (value: unknown): Record<string, unknown> => isRecord(value) ? value : {};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
