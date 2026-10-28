import { randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join } from "node:path";
import { z } from "zod";
import { AguiClient } from "./protocol/agui-client.js";
import { V2SessionClient } from "./protocol/v2-session-client.js";
import { store } from "./state/store.js";
import { initLogger } from "./utils/logger.js";
import type { AppExitReason, RenderAppOptions } from "./index.js";
import { configuredListen, formatListenBase, readHttpListen } from "./v2-backend.js";

export const V2_COMMANDS = ["/help", "/clear", "/resume", "/outputs", "/exit"];

/** Accept `http://<server.host>:<server.port>` or the same origin plus `/dataagent/stream`. */
export function normalizeRuntimeUrl(value: string, env: NodeJS.ProcessEnv = process.env): string {
  const listen = configuredListen(env);
  const expected = formatListenBase(listen.host, listen.port);
  const target = readHttpListen(value);
  const allowed = new Set([listen.host, "127.0.0.1"]);
  if (!target || !allowed.has(target.host)) {
    throw new Error(`V2 requires the configured listen URL: ${expected}`);
  }
  return `${formatListenBase(target.host, target.port)}/dataagent/stream`;
}

/** Keep V2 requests on the configured backend and reject redirects. */
export function v2Transport(
  runtimeUrl: string,
  fetchImpl: typeof fetch,
  env: NodeJS.ProcessEnv = process.env,
): typeof fetch {
  const runtime = new URL(normalizeRuntimeUrl(runtimeUrl, env));
  return async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    if (url.origin !== runtime.origin) throw new Error("Refusing to send a V2 request to another origin");
    const headers = new Headers(init?.headers);
    return fetchImpl(input, { ...init, headers, redirect: "error" });
  };
}

/** Log path comes only from this machine, never from the health response. */
export function resolveTuiLogPath(env: NodeJS.ProcessEnv = process.env): string {
  const explicit = env.DATAAGENT_V2_LOG_PATH?.trim();
  if (explicit) {
    if (!isAbsolute(explicit)) throw new Error("V2 log path must be absolute");
    return explicit;
  }
  const envHome = env.DATAAGENT_HOME?.trim();
  if (envHome && isAbsolute(envHome)) return join(envHome, "runtime", "logs", "tui.log");
  return join(homedir(), ".datafoundry", "tui.log");
}

/** Accept the control-plane envelope and the standalone `dataagent serve` health body. */
export function v2HealthPayload(body: unknown): unknown {
  if (!body || typeof body !== "object" || !("success" in body)) return body;
  const record = body as { success?: unknown; data?: unknown };
  if (record.success !== true || !record.data || typeof record.data !== "object") {
    throw new Error("Invalid control-plane health response");
  }
  const conversation = (record.data as { conversation?: unknown }).conversation;
  if (!conversation || typeof conversation !== "object" || (conversation as { ready?: unknown }).ready !== true) {
    throw new Error(
      "Control plane is up, but the conversation engine is not ready.",
    );
  }
  return conversation;
}

export async function runV2Tui(options: {
  runtimeUrl: string;
  fetchImpl: typeof fetch;
  renderApp: (options: RenderAppOptions) => Promise<AppExitReason>;
  env?: NodeJS.ProcessEnv | undefined;
  instanceId?: string | undefined;
  shutdownSignal?: AbortSignal | undefined;
  initialResume?: { enabled: boolean; sessionId?: string | undefined } | undefined;
}): Promise<number> {
  const transport = v2Transport(options.runtimeUrl, options.fetchImpl, options.env ?? process.env);
  const baseUrl = new URL(options.runtimeUrl).origin;
  let response: Response;
  try {
    response = await transport(baseUrl + "/healthz", { signal: AbortSignal.any([
      AbortSignal.timeout(10_000), ...(options.shutdownSignal ? [options.shutdownSignal] : []),
    ]) });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(
      `Control plane is not reachable at ${baseUrl}. Start it with npm run start or ./deploy.sh start. ${detail}`,
    );
  }
  if (!response.ok) throw new Error(`Control plane is unavailable (${response.status}) at ${baseUrl}/healthz`);
  const parsed = z.object({
    protocol: z.literal("dataagent-v2"), status: z.literal("ready"), instanceId: z.string(),
    model: z.string(), home: z.string().refine(isAbsolute).optional(),
    workspaces: z.array(z.object({
      name: z.string().min(1), path: z.string().refine(isAbsolute),
    }).strict()).optional(),
    user: z.string().optional(),
    stateDir: z.string().refine(isAbsolute).optional(), timeoutSeconds: z.number().positive(),
  }).safeParse(v2HealthPayload(await response.json()));
  if (!parsed.success) throw new Error("Invalid V2 health response");
  const health = parsed.data;
  const identity = options.instanceId ?? process.env.DATAAGENT_V2_INSTANCE_ID;
  if (identity && health.instanceId !== identity) {
    throw new Error("Backend instance identity does not match the launcher");
  }
  const logPath = resolveTuiLogPath();
  initLogger({ logDir: dirname(logPath), logFileName: basename(logPath), debugMode: false }).info("V2 terminal connected");
  store.setWorkspaceConfig({ llm: [], db: [], skill: [], kb: [], mcp: [] });
  store.startNewSession(randomUUID());
  store.setConnectionStatus("connected");
  const client = new AguiClient(options.runtimeUrl, transport, (health.timeoutSeconds + 10) * 1000);
  try {
    await options.renderApp({
      client, datasourceId: undefined, initialDatasourceId: undefined, onExit: () => {},
      v2: { sessions: new V2SessionClient(baseUrl, transport), model: health.model, home: health.home ?? "",
        mounts: health.workspaces?.map((item) => `${item.name}: ${item.path}`).join("  ") },
      shutdownSignal: options.shutdownSignal,
      ...(options.initialResume ? { initialResume: options.initialResume } : {}),
    });
    return 0;
  } finally {
    client.dispose();
  }
}
