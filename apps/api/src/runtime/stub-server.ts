import { createServer as createHttpServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";

import { generateStubEvents } from "./scenarios.js";
import { encodeAgUiSseEvent } from "./sse.js";
import {
  RUNTIME_CONTRACT_VERSION,
  RUNTIME_PROVIDER,
  type RuntimeRunRequest
} from "./types.js";

export type CreateRuntimeStubServerOptions = {
  token?: string;
};

const canceledRuns = new Set<string>();

export const createRuntimeStubServer = (options: CreateRuntimeStubServerOptions = {}): Server =>
  createHttpServer(async (request, response) => {
    const url = new URL(request.url ?? "/", `http://${request.headers.host ?? "127.0.0.1"}`);
    if (options.token && request.headers.authorization !== `Bearer ${options.token}`) {
      sendJson(response, 401, { error: "UNAUTHORIZED" });
      return;
    }

    if (request.method === "GET" && url.pathname === "/health") {
      sendJson(response, 200, {
        status: "ok",
        provider: `${RUNTIME_PROVIDER}-stub`,
        version: RUNTIME_CONTRACT_VERSION,
        capabilities: { streaming: true, tools: true, interrupt: true, cancel: true }
      });
      return;
    }

    const cancelMatch = url.pathname.match(/^\/runs\/([^/]+)\/cancel$/);
    if (request.method === "POST" && cancelMatch) {
      canceledRuns.add(decodeURIComponent(cancelMatch[1] ?? ""));
      sendJson(response, 200, { canceled: true });
      return;
    }

    if (request.method === "POST" && url.pathname === "/runs/stream") {
      const body = await readJson(request);
      const runRequest = body as RuntimeRunRequest;
      if (!runRequest?.runId || !runRequest.threadId || !Array.isArray(runRequest.messages)) {
        sendJson(response, 400, { error: "INVALID_RUNTIME_RUN_REQUEST" });
        return;
      }
      canceledRuns.delete(runRequest.runId);
      const payload = [...generateStubEvents(runRequest)]
        .map((event) => encodeAgUiSseEvent(event))
        .join("");
      response.writeHead(200, {
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
        "Content-Length": Buffer.byteLength(payload),
        "Content-Type": "text/event-stream; charset=utf-8",
        "X-Accel-Buffering": "no"
      });
      response.end(payload);
      return;
    }

    sendJson(response, 404, { error: "NOT_FOUND" });
  });

const readJson = async (request: IncomingMessage): Promise<unknown> => {
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  const raw = Buffer.concat(chunks).toString("utf8");
  return raw ? JSON.parse(raw) as unknown : {};
};

const sendJson = (response: ServerResponse, status: number, body: unknown): void => {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(body));
};
