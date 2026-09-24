import { randomUUID } from "node:crypto";
import { basename, dirname, isAbsolute } from "node:path";
import { z } from "zod";
import { AguiClient } from "./protocol/agui-client.js";
import { V2SessionClient } from "./protocol/v2-session-client.js";
import { store } from "./state/store.js";
import { initLogger } from "./utils/logger.js";
import type { RenderAppOptions } from "./index.js";
import type { AppExitReason } from "./auth/types.js";

export const V2_COMMANDS = ["/help", "/clear", "/resume", "/outputs", "/exit"];

/** Keep V2 requests on the loopback backend and reject redirects. */
export function v2Transport(runtimeUrl: string, fetchImpl: typeof fetch): typeof fetch {
  const runtime = new URL(runtimeUrl);
  if (runtime.protocol !== "http:" || runtime.hostname !== "127.0.0.1"
    || runtime.pathname !== "/dataagent/stream" || runtime.username || runtime.password
    || runtime.search || runtime.hash) {
    throw new Error("V2 requires a loopback URL: http://127.0.0.1:<port>/dataagent/stream");
  }
  return async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    if (url.origin !== runtime.origin) throw new Error("Refusing to send a V2 request to another origin");
    const headers = new Headers(init?.headers);
    return fetchImpl(input, { ...init, headers, redirect: "error" });
  };
}

export async function runV2Tui(options: {
  runtimeUrl: string;
  fetchImpl: typeof fetch;
  renderApp: (options: RenderAppOptions) => Promise<AppExitReason>;
  instanceId?: string | undefined;
  shutdownSignal?: AbortSignal | undefined;
  initialResume?: { enabled: boolean; sessionId?: string | undefined } | undefined;
}): Promise<number> {
  const transport = v2Transport(options.runtimeUrl, options.fetchImpl);
  const baseUrl = new URL(options.runtimeUrl).origin;
  const response = await transport(baseUrl + "/healthz", { signal: AbortSignal.any([
    AbortSignal.timeout(10_000), ...(options.shutdownSignal ? [options.shutdownSignal] : []),
  ]) });
  if (!response.ok) throw new Error(`V2 backend is unavailable (${response.status})`);
  const parsed = z.object({
    protocol: z.literal("dataagent-v2"), status: z.literal("ready"), instanceId: z.string(),
    model: z.string(), home: z.string().refine(isAbsolute),
    workspaces: z.array(z.object({
      name: z.string().min(1), path: z.string().refine(isAbsolute),
    }).strict()).optional(),
    user: z.string().optional(),
    stateDir: z.string().refine(isAbsolute), timeoutSeconds: z.number().positive(),
  }).safeParse(await response.json());
  if (!parsed.success) throw new Error("Invalid V2 health response: absolute home and stateDir are required");
  const health = parsed.data;
  const identity = options.instanceId ?? process.env.DATAAGENT_V2_INSTANCE_ID;
  if (identity && health.instanceId !== identity) {
    throw new Error("Backend instance identity does not match the launcher");
  }
  const logPath = process.env.DATAAGENT_V2_LOG_PATH ?? `${health.stateDir}/logs/tui.log`;
  if (!isAbsolute(logPath)) throw new Error("V2 log path must be absolute");
  initLogger({ logDir: dirname(logPath), logFileName: basename(logPath), debugMode: false }).info("V2 terminal connected");
  store.setWorkspaceConfig({ llm: [], db: [], skill: [], kb: [], mcp: [] });
  store.startNewSession(randomUUID());
  store.setConnectionStatus("connected");
  const client = new AguiClient(options.runtimeUrl, transport, (health.timeoutSeconds + 10) * 1000);
  try {
    await options.renderApp({
      client, datasourceId: undefined, initialDatasourceId: undefined, onExit: () => {},
      v2: { sessions: new V2SessionClient(baseUrl, transport), model: health.model, home: health.home,
        mounts: health.workspaces?.map((item) => `${item.name}: ${item.path}`).join("  ") },
      shutdownSignal: options.shutdownSignal,
      ...(options.initialResume ? { initialResume: options.initialResume } : {}),
    });
    return 0;
  } finally {
    client.dispose();
  }
}
