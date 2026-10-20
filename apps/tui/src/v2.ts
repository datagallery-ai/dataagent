import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { basename, dirname, isAbsolute, join } from "node:path";
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
  instanceId?: string | undefined;
  shutdownSignal?: AbortSignal | undefined;
  initialResume?: { enabled: boolean; sessionId?: string | undefined } | undefined;
}): Promise<number> {
  const transport = v2Transport(options.runtimeUrl, options.fetchImpl);
  const baseUrl = new URL(options.runtimeUrl).origin;
  let response: Response;
  try {
    response = await transport(baseUrl + "/healthz", { signal: AbortSignal.any([
      AbortSignal.timeout(10_000), ...(options.shutdownSignal ? [options.shutdownSignal] : []),
    ]) });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(
      `Control plane is not reachable at ${baseUrl}. Start it with npm start (one Python process on that address). The TUI does not start a second backend. ${detail}`,
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
  if (!parsed.success) throw new Error("Invalid V2 health response: absolute home and stateDir are required");
  const health = parsed.data;
  const identity = options.instanceId ?? process.env.DATAAGENT_V2_INSTANCE_ID;
  if (identity && health.instanceId !== identity) {
    throw new Error("Backend instance identity does not match the launcher");
  }
  const envHome = process.env.DATAAGENT_HOME?.trim();
  const stateDir = health.stateDir
    ?? (envHome && isAbsolute(envHome) ? join(envHome, "runtime") : undefined);
  const logPath = process.env.DATAAGENT_V2_LOG_PATH
    ?? (stateDir ? join(stateDir, "logs", "tui.log") : join(tmpdir(), "dataagent-tui.log"));
  if (!isAbsolute(logPath)) throw new Error("V2 log path must be absolute");
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
