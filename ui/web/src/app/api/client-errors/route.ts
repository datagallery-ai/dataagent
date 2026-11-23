import { z } from "zod";
import { webLogger } from "../../../lib/web-logger";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
const payload = z.object({
  message: z.string().min(1).max(4000), stack: z.string().max(16000).optional(),
  page: z.string().max(500).optional(), stage: z.string().max(40).optional(),
  code: z.string().max(80).optional(),
  threadId: z.string().max(128).optional(), runId: z.string().max(128).optional(),
  requestId: z.string().max(128).optional(),
}).strict();
let budget = { start: 0, count: 0, bytes: 0 };

export async function POST(request: Request) {
  const origin = request.headers.get("origin");
  const expectedOrigin = new URL(process.env.DATAAGENT_WEB_ORIGIN || process.env.__NEXT_PRIVATE_ORIGIN || request.url).origin;
  const site = request.headers.get("sec-fetch-site");
  if (origin !== expectedOrigin || (site !== null && site !== "same-origin") ||
      !request.headers.get("content-type")?.startsWith("application/json")) {
    return new Response(null, { status: 403 });
  }
  const now = Date.now();
  if (now - budget.start >= 60_000) budget = { start: now, count: 0, bytes: 0 };
  if (budget.count >= 60 || budget.bytes >= 1_048_576) return new Response(null, { status: 429 });
  const reader = request.body?.getReader();
  let text = "";
  let size = 0;
  const decoder = new TextDecoder();
  try {
    if (!reader) return new Response(null, { status: 400 });
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 32_768) { await reader.cancel(); return new Response(null, { status: 413 }); }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    const parsed = payload.safeParse(JSON.parse(text));
    if (!parsed.success) return new Response(null, { status: 400 });
    // Body reads yield; reserve the process-wide quota immediately before the synchronous write.
    if (budget.count >= 60 || budget.bytes + size > 1_048_576) return new Response(null, { status: 429 });
    budget.count++;
    budget.bytes += size;
    const { message, ...metadata } = parsed.data;
    webLogger.error(message, { ...metadata, component: "browser", event: "client_error" });
    return new Response(null, { status: 204 });
  } catch { return new Response(null, { status: 400 }); }
  finally { reader?.releaseLock(); }
}
