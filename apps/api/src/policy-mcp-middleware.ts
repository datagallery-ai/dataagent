import {
  EventType,
  Middleware,
  type AbstractAgent,
  type BaseEvent,
  type Message,
  type RunAgentInput,
  type Tool,
  type ToolCall
} from "@ag-ui/client";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { SSEClientTransport } from "@modelcontextprotocol/sdk/client/sse.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import { randomUUID } from "node:crypto";
import { Observable } from "rxjs";

export type PolicyMcpToolConfig = {
  description?: string | undefined;
  inputSchema?: unknown;
  name: string;
};

export type PolicyMcpClientConfig = {
  serverId: string;
  timeoutMs?: number;
  toolAllowlist?: string[];
  tools?: PolicyMcpToolConfig[];
} & (
  | {
      headers?: Record<string, string>;
      type: "http" | "sse";
      url: string;
    }
  | {
      args?: string[];
      command: string;
      cwd?: string;
      env?: Record<string, string>;
      type: "stdio";
    }
);

type ListedMcpTool = {
  mcpTool: {
    description?: string | undefined;
    inputSchema?: unknown;
    name: string;
  };
  serverConfig: PolicyMcpClientConfig;
  serverId: string;
};

type ResolvedMcpTool = {
  originalName: string;
  serverConfig: PolicyMcpClientConfig;
  tool: Tool;
};

const MAX_TOOL_NAME_LENGTH = 64;
const DEFAULT_MAX_ITERATIONS = 32;
const DEFAULT_MCP_TIMEOUT_MS = 30000;

/** AG-UI MCP loop with runtime tool allowlist and timeout policy applied before tool injection. */
export class PolicyMcpMiddleware extends Middleware {
  private listingPromise: Promise<ListedMcpTool[]> | null = null;
  private readonly maxIterations: number;
  private readonly mcpServers: PolicyMcpClientConfig[];

  constructor(mcpServers: PolicyMcpClientConfig[] = [], options: { maxIterations?: number } = {}) {
    super();
    this.mcpServers = mcpServers;
    const maxIterations = options.maxIterations ?? DEFAULT_MAX_ITERATIONS;
    this.maxIterations = Number.isFinite(maxIterations)
      ? Math.max(1, Math.floor(maxIterations))
      : DEFAULT_MAX_ITERATIONS;
  }

  run(input: RunAgentInput, next: AbstractAgent): Observable<BaseEvent> {
    if (this.mcpServers.length === 0) {
      return this.runNext(input, next);
    }
    return new Observable<BaseEvent>((subscriber) => {
      let cancelled = false;
      let activeSubscription: { unsubscribe(): void } | undefined;
      let iterations = 0;

      const continueRun = (
        runInput: RunAgentInput,
        toolMap: Map<string, ResolvedMcpTool>,
        suppressRunStarted: boolean
      ): void => {
        let currentMessages = runInput.messages;
        let failed = false;
        let pendingFinished: BaseEvent | null = null;
        activeSubscription = this.runNextWithState(runInput, next).subscribe({
          next: ({ event, messages }) => {
            currentMessages = messages;
            if (event.type === EventType.RUN_ERROR) {
              failed = true;
              subscriber.next(event);
              return;
            }
            if (event.type === EventType.RUN_FINISHED) {
              pendingFinished = event;
              return;
            }
            if (event.type !== EventType.RUN_STARTED || !suppressRunStarted) {
              subscriber.next(event);
            }
          },
          error: (error) => subscriber.error(error),
          complete: () => {
            void this.handleIterationComplete({
              cancelled: () => cancelled,
              currentMessages,
              failed,
              iterations: () => iterations,
              nextIteration: () => {
                iterations += 1;
              },
              pendingFinished,
              runInput,
              subscriber,
              toolMap,
              continueRun
            }).catch((error: unknown) => subscriber.error(error));
          }
        });
      };

      void this.resolveTools(new Set(input.tools.map((tool) => tool.name)))
        .then((tools) => {
          if (cancelled) {
            return;
          }
          const toolMap = new Map(tools.map((tool) => [tool.tool.name, tool]));
          continueRun({ ...input, tools: [...input.tools, ...tools.map((tool) => tool.tool)] }, toolMap, false);
        })
        .catch((error: unknown) => subscriber.error(error));

      return () => {
        cancelled = true;
        activeSubscription?.unsubscribe();
      };
    });
  }

  private async handleIterationComplete(input: {
    cancelled(): boolean;
    continueRun(runInput: RunAgentInput, toolMap: Map<string, ResolvedMcpTool>, suppressRunStarted: boolean): void;
    currentMessages: Message[];
    failed: boolean;
    iterations(): number;
    nextIteration(): void;
    pendingFinished: BaseEvent | null;
    runInput: RunAgentInput;
    subscriber: { complete(): void; next(event: BaseEvent): void };
    toolMap: Map<string, ResolvedMcpTool>;
  }): Promise<void> {
    if (input.cancelled()) {
      return;
    }
    if (input.failed) {
      input.subscriber.complete();
      return;
    }
    const openCalls = openToolCalls(input.currentMessages)
      .filter((toolCall) => input.toolMap.has(toolCall.function.name));
    if (openCalls.length === 0) {
      if (input.pendingFinished) {
        input.subscriber.next(input.pendingFinished);
      }
      input.subscriber.complete();
      return;
    }
    if (input.iterations() >= this.maxIterations) {
      console.warn(`[PolicyMcpMiddleware] Reached maxIterations (${this.maxIterations}); stopping MCP calls.`);
      // Emit an explicit, authoritative terminal result for every open MCP call so the
      // frontend can render "stopped: MCP iteration limit" instead of a call that hangs
      // pending until the run's TOOL_RESULT_NOT_DELIVERED safety net fires.
      const limitContent = JSON.stringify({
        status: "error",
        error: `MCP_ITERATION_LIMIT:${this.maxIterations}`
      });
      for (const toolCall of openCalls) {
        input.subscriber.next({
          type: EventType.TOOL_CALL_RESULT,
          messageId: randomUUID(),
          toolCallId: toolCall.id,
          toolCallName: toolCall.function.name,
          content: limitContent,
          role: "tool"
        });
      }
      if (input.pendingFinished) {
        input.subscriber.next(input.pendingFinished);
      }
      input.subscriber.complete();
      return;
    }
    input.nextIteration();
    const results = await Promise.all(openCalls.map(async (toolCall) => {
      const resolved = input.toolMap.get(toolCall.function.name) as ResolvedMcpTool;
      return { content: await this.executeToolCall(resolved, toolCall), toolCall };
    }));
    if (input.cancelled()) {
      return;
    }
    const toolMessages = results.map(({ content, toolCall }) => {
      const messageId = randomUUID();
      input.subscriber.next({
        type: EventType.TOOL_CALL_RESULT,
        messageId,
        toolCallId: toolCall.id,
        toolCallName: toolCall.function.name,
        content,
        role: "tool"
      });
      return { id: messageId, role: "tool" as const, content, toolCallId: toolCall.id };
    });
    const nextMessages = [...input.currentMessages, ...toolMessages];
    if (openToolCalls(nextMessages).length > 0) {
      if (input.pendingFinished) {
        input.subscriber.next(input.pendingFinished);
      }
      input.subscriber.complete();
      return;
    }
    input.runInput.messages.push(...toolMessages);
    input.continueRun({ ...input.runInput, runId: randomUUID(), messages: nextMessages }, input.toolMap, true);
  }

  private async resolveTools(existingNames: Set<string>): Promise<ResolvedMcpTool[]> {
    const listedTools = await this.listAllTools();
    const usedNames = new Set(existingNames);
    return listedTools.map((listed) => {
      const name = resolveMcpToolName(listed.serverId, listed.mcpTool.name, usedNames);
      usedNames.add(name);
      return {
        tool: {
          name,
          description: listed.mcpTool.description ?? "",
          parameters: listed.mcpTool.inputSchema ?? { type: "object", properties: {} }
        },
        originalName: listed.mcpTool.name,
        serverConfig: listed.serverConfig
      };
    });
  }

  private listAllTools(): Promise<ListedMcpTool[]> {
    if (this.listingPromise === null) {
      this.listingPromise = this.doListAllTools();
    }
    return this.listingPromise;
  }

  private async doListAllTools(): Promise<ListedMcpTool[]> {
    const tools: ListedMcpTool[] = [];
    for (const serverConfig of this.mcpServers) {
      let client: Client | undefined;
      try {
        client = await this.connect(serverConfig);
        const result = await withTimeout(
          client.listTools(),
          serverConfig.timeoutMs ?? DEFAULT_MCP_TIMEOUT_MS,
          `listTools:${serverConfig.serverId}`
        );
        for (const tool of result.tools) {
          if (matchesToolAllowlist(serverConfig, tool.name)) {
            tools.push({ mcpTool: tool, serverConfig, serverId: serverConfig.serverId });
          }
        }
      } catch (error) {
        console.error(`[PolicyMcpMiddleware] Failed to list tools from MCP server ${serverConfig.serverId}:`, error);
      } finally {
        await closeClient(client);
      }
    }
    return tools;
  }

  private async executeToolCall(resolved: ResolvedMcpTool, toolCall: ToolCall): Promise<string> {
    let args: Record<string, unknown> = {};
    try {
      args = toolCall.function.arguments ? JSON.parse(toolCall.function.arguments) : {};
    } catch {
      console.warn(`[PolicyMcpMiddleware] Malformed JSON arguments for ${resolved.originalName}; using empty args.`);
    }
    return callPolicyMcpTool(resolved.serverConfig, resolved.originalName, args);
  }

  private async connect(serverConfig: PolicyMcpClientConfig): Promise<Client> {
    const client = new Client({ name: "datafoundry-mcp-policy", version: "0.2.0" });
    const transport = createTransport(serverConfig);
    await client.connect(transport);
    return client;
  }
}

const createTransport = (serverConfig: PolicyMcpClientConfig): Transport => {
  if (serverConfig.type === "stdio") {
    return new StdioClientTransport({
      command: serverConfig.command,
      ...(serverConfig.args ? { args: serverConfig.args } : {}),
      ...(serverConfig.cwd ? { cwd: serverConfig.cwd } : {}),
      ...(serverConfig.env ? { env: serverConfig.env } : {}),
      stderr: "pipe"
    });
  }
  const requestOptions = serverConfig.headers ? { requestInit: { headers: serverConfig.headers } } : undefined;
  return (serverConfig.type === "sse"
    ? new SSEClientTransport(new URL(serverConfig.url), requestOptions)
    : new StreamableHTTPClientTransport(new URL(serverConfig.url), requestOptions)) as unknown as Transport;
};

export const callPolicyMcpTool = async (
  serverConfig: PolicyMcpClientConfig,
  toolName: string,
  args: Record<string, unknown>
): Promise<string> => {
  let client: Client | undefined;
  try {
    client = await connectPolicyMcpClient(serverConfig);
    const result = await withTimeout(
      client.callTool({ name: toolName, arguments: args }),
      serverConfig.timeoutMs ?? DEFAULT_MCP_TIMEOUT_MS,
      `callTool:${serverConfig.serverId}:${toolName}`
    );
    return stringifyMcpResult(result);
  } catch (error) {
    console.error(`[PolicyMcpMiddleware] Tool execution failed for ${toolName}:`, error);
    return `Error executing tool ${toolName}: ${String(error)}`;
  } finally {
    await closeClient(client);
  }
};

export const connectPolicyMcpClient = async (serverConfig: PolicyMcpClientConfig): Promise<Client> => {
  const client = new Client({ name: "datafoundry-mcp-policy", version: "0.2.0" });
  const transport = createTransport(serverConfig);
  await client.connect(transport);
  return client;
};

const openToolCalls = (messages: Message[]): ToolCall[] => {
  const toolCalls: ToolCall[] = [];
  const completed = new Set<string>();
  for (const message of messages) {
    if (message.role === "assistant" && Array.isArray(message.toolCalls)) {
      toolCalls.push(...message.toolCalls);
    }
    if (message.role === "tool") {
      completed.add(message.toolCallId);
    }
  }
  return toolCalls.filter((toolCall) => !completed.has(toolCall.id));
};

const matchesToolAllowlist = (serverConfig: PolicyMcpClientConfig, toolName: string): boolean => {
  if (!serverConfig.toolAllowlist || serverConfig.toolAllowlist.length === 0) {
    return true;
  }
  return mcpToolAllowlistCandidates(toolName).some((candidate) => {
    const baseName = `mcp__${sanitizeMcpName(serverConfig.serverId)}__${sanitizeMcpName(candidate)}`;
    return serverConfig.toolAllowlist?.includes(candidate) || serverConfig.toolAllowlist?.includes(baseName);
  });
};

const mcpToolAllowlistCandidates = (toolName: string): string[] => {
  if (toolName === "datalink_show" || toolName === "datagraph_show") {
    return ["datalink_show", "datagraph_show"];
  }
  if (toolName === "datalink_explore" || toolName === "datagraph_explore") {
    return ["datalink_explore", "datagraph_explore"];
  }
  return [toolName];
};

const resolveMcpToolName = (serverId: string, toolName: string, usedNames: Set<string>): string => {
  if (toolName.length === 0 || toolName.length > MAX_TOOL_NAME_LENGTH || !/^[a-zA-Z0-9_-]+$/u.test(toolName)) {
    throw new Error(`MCP_TOOL_NAME_INVALID:${serverId}:${toolName}`);
  }
  if (usedNames.has(toolName)) {
    throw new Error(`MCP_TOOL_NAME_CONFLICT:${serverId}:${toolName}`);
  }
  return toolName;
};

const stringifyMcpResult = (result: unknown): string => {
  const record = result as { content?: unknown };
  if (Array.isArray(record.content)) {
    const text = record.content
      .filter((item): item is { text: string; type: "text" } =>
        typeof item === "object" && item !== null && (item as { type?: unknown }).type === "text"
        && typeof (item as { text?: unknown }).text === "string")
      .map((item) => item.text)
      .join("\n");
    return text || JSON.stringify(record.content);
  }
  return JSON.stringify(record.content ?? result);
};

const withTimeout = async <T>(promise: Promise<T>, timeoutMs: number, label: string): Promise<T> => {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<T>((_, reject) => {
        timer = setTimeout(() => reject(new Error(`MCP_TIMEOUT:${label}:${timeoutMs}`)), timeoutMs);
      })
    ]);
  } finally {
    if (timer) {
      clearTimeout(timer);
    }
  }
};

const closeClient = async (client: Client | undefined): Promise<void> => {
  if (!client) {
    return;
  }
  await client.close().catch((error: unknown) => {
    console.error("[PolicyMcpMiddleware] Failed to close MCP client:", error);
  });
};

const sanitizeMcpName = (value: string): string => value.replace(/[^a-zA-Z0-9_-]/gu, "_");
