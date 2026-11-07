import { randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join } from "node:path";
import { z } from "zod";
import { AguiClient } from "./protocol/agui-client.js";
import { SessionClient } from "./protocol/session-client.js";
import { store } from "./state/store.js";
import { initLogger } from "./utils/logger.js";
import type { AppExitReason, RenderAppOptions } from "./index.js";
import { configuredListen, formatListenBase, readHttpListen } from "./backend.js";

export const TUI_COMMANDS = ["/help", "/clear", "/stop", "/resume", "/outputs", "/exit"];

/** Bind-all addresses are not client destinations. Connect through loopback. */
export function clientListenHost(host: string): string {
  if (host === "0.0.0.0") return "127.0.0.1";
  if (host === "::" || host === "::0") return "::1";
  return host;
}

function isIpLiteral(host: string): boolean {
  if (host.includes(":")) return /^[0-9a-f:.]+$/i.test(host);
  const parts = host.split(".");
  return parts.length === 4 && parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255);
}

/** Accept `http://<server.host>:<server.port>` or the same origin plus `/dataagent/stream`. */
export function normalizeRuntimeUrl(value: string, env: NodeJS.ProcessEnv = process.env): string {
  const listen = configuredListen(env);
  const expected = formatListenBase(listen.host, listen.port);
  const target = readHttpListen(value);
  if (!target) throw new Error(`The TUI requires the configured listen URL: ${expected}`);
  const host = clientListenHost(target.host);
  const allowed = new Set([
    listen.host,
    clientListenHost(listen.host),
    "127.0.0.1",
    "::1",
    "localhost",
  ]);
  if (!allowed.has(host) && !isIpLiteral(host)) {
    throw new Error(`The TUI requires the configured listen URL: ${expected}`);
  }
  return `${formatListenBase(host, target.port)}/dataagent/stream`;
}

/** Keep requests on the configured backend and reject redirects. */
export function sessionTransport(
  runtimeUrl: string,
  fetchImpl: typeof fetch,
  env: NodeJS.ProcessEnv = process.env,
): typeof fetch {
  const runtime = new URL(normalizeRuntimeUrl(runtimeUrl, env));
  return async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    if (url.origin !== runtime.origin) throw new Error("Refusing to send a request to another origin");
    const headers = new Headers(init?.headers);
    return fetchImpl(input, { ...init, headers, redirect: "error" });
  };
}

/** Log path comes only from this machine, never from the health response. */
export function resolveTuiLogPath(env: NodeJS.ProcessEnv = process.env): string {
  const explicit = env.DATAAGENT_TUI_LOG_PATH?.trim();
  if (explicit) {
    if (!isAbsolute(explicit)) throw new Error("TUI log path must be absolute");
    return explicit;
  }
  const envHome = env.DATAAGENT_HOME?.trim();
  if (envHome && isAbsolute(envHome)) return join(envHome, "runtime", "logs", "tui.log");
  return join(homedir(), ".datagallery", "tui.log");
}

/** Accept the control-plane envelope and the standalone `dataagent serve` health body. */
export function healthPayload(body: unknown): unknown {
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

export async function connectTui(options: {
  runtimeUrl: string;
  fetchImpl: typeof fetch;
  renderApp: (options: RenderAppOptions) => Promise<AppExitReason>;
  env?: NodeJS.ProcessEnv | undefined;
  instanceId?: string | undefined;
  shutdownSignal?: AbortSignal | undefined;
  initialResume?: { enabled: boolean; sessionId?: string | undefined } | undefined;
}): Promise<number> {
  const env = options.env ?? process.env;
  // A self-started backend advertises its bind address. Health, the stream, and
  // session calls share one client URL, including 0.0.0.0 and :: rewritten to loopback.
  const runtimeUrl = normalizeRuntimeUrl(options.runtimeUrl, env);
  const transport = sessionTransport(runtimeUrl, options.fetchImpl, env);
  const baseUrl = new URL(runtimeUrl).origin;
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
    protocol: z.literal("dataagent"), status: z.literal("ready"), instanceId: z.string(),
    model: z.string(), home: z.string().refine(isAbsolute).optional(),
    workspaces: z.array(z.object({
      name: z.string().min(1), path: z.string().refine(isAbsolute),
    }).strict()).optional(),
    user: z.string().optional(),
    stateDir: z.string().refine(isAbsolute).optional(), timeoutSeconds: z.number().positive(),
  }).safeParse(healthPayload(await response.json()));
  if (!parsed.success) throw new Error("Invalid health response");
  const health = parsed.data;
  const identity = options.instanceId ?? process.env.DATAAGENT_INSTANCE_ID;
  if (identity && health.instanceId !== identity) {
    throw new Error("Backend instance identity does not match the launcher");
  }
  const logPath = resolveTuiLogPath();
  initLogger({ logDir: dirname(logPath), logFileName: basename(logPath), debugMode: false }).info("Terminal connected");
  store.setWorkspaceConfig({ llm: [], db: [], skill: [], kb: [], mcp: [] });
  store.startNewSession(randomUUID());
  store.setConnectionStatus("connected");
  const client = new AguiClient(runtimeUrl, transport, (health.timeoutSeconds + 10) * 1000);
  try {
    await options.renderApp({
      client, datasourceId: undefined, initialDatasourceId: undefined, onExit: () => {},
      tui: { sessions: new SessionClient(baseUrl, transport), model: health.model, home: health.home ?? "",
        mounts: health.workspaces?.map((item) => `${item.name}: ${item.path}`).join("  ") },
      shutdownSignal: options.shutdownSignal,
      ...(options.initialResume ? { initialResume: options.initialResume } : {}),
    });
    return 0;
  } finally {
    client.dispose();
  }
}
