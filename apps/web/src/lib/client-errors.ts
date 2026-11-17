import { redactLogText } from "../../../../scripts/lib/log-redaction.mjs";

type ErrorContext = { threadId?: string; runId?: string; requestId?: string; stage?: string; code?: string };

let sent = 0;
let windowStart = 0;
const recent = new Map<string | Error, number>();

/** Reporting must never break the UI or recursively report its own failures. */
export function reportClientError(error: unknown, context: ErrorContext = {}): void {
  if (typeof window === "undefined") return;
  const message = error instanceof Error ? error.message : String(error);
  const stack = error instanceof Error ? error.stack : undefined;
  const now = Date.now();
  if (now - windowStart > 60_000) { windowStart = now; sent = 0; recent.clear(); }
  const key = `${context.threadId ?? ""}:${context.runId ?? ""}:${message}`;
  if (sent >= 20 || now - (recent.get(key) ?? 0) < 5000 ||
      (error instanceof Error && now - (recent.get(error) ?? 0) < 5000)) return;
  recent.set(key, now);
  if (error instanceof Error) recent.set(error, now);
  sent++;
  try {
    const metadata = Object.fromEntries(Object.entries(context).filter(([, value]) => typeof value === "string"));
    const payload = { message: redactLogText(message).slice(0, 4000),
      stack: stack ? redactLogText(stack).slice(0, 16000) : undefined,
      ...metadata, page: window.location.pathname.slice(0, 500) };
    let body = JSON.stringify(payload);
    while (new TextEncoder().encode(body).byteLength > 32_768 && payload.stack) {
      payload.stack = payload.stack.slice(0, Math.floor(payload.stack.length / 2));
      body = JSON.stringify(payload);
    }
    void fetch("/api/client-errors", {
      method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
      body,
    }).catch(() => {});
  } catch { /* Network failures leave diagnostics best effort. */ }
}
