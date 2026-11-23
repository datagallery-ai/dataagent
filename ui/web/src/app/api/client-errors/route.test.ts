import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { error } = vi.hoisted(() => ({ error: vi.fn() }));
vi.mock("../../../lib/web-logger", () => ({ webLogger: { error } }));
let POST: typeof import("./route")["POST"];

function request(body: unknown, origin = "http://web.test") {
  return new Request("http://web.test/api/client-errors", { method: "POST",
    headers: { "content-type": "application/json", origin }, body: JSON.stringify(body) });
}

describe("browser error ingestion uses the shared web logger", () => {
  beforeEach(async () => {
    vi.resetModules();
    vi.stubEnv("DATAAGENT_WEB_ORIGIN", "");
    vi.stubEnv("__NEXT_PRIVATE_ORIGIN", "");
    error.mockClear();
    ({ POST } = await import("./route"));
  });
  afterEach(() => { vi.unstubAllEnvs(); vi.useRealTimers(); });
  it("preserves correlation and stack, fixing the destination and component", async () => {
    const response = await POST(request({ message: "render failed", stack: "frame\nlocation",
      threadId: "thread-1", runId: "run-1", stage: "render" }));
    expect(response.status).toBe(204);
    expect(error).toHaveBeenCalledWith("render failed", expect.objectContaining({
      component: "browser", threadId: "thread-1", runId: "run-1", stack: "frame\nlocation",
    }));
  });
  it("rejects cross-origin, oversized and arbitrary fields without logging them", async () => {
    expect((await POST(request({ message: "untrusted" }, "https://other.test"))).status).toBe(403);
    expect((await POST(request({ message: "untrusted", component: "backend" }))).status).toBe(400);
    expect((await POST(request({ message: "x".repeat(40_000) }))).status).toBe(413);
    expect(error).not.toHaveBeenCalled();
  });
  it("bounds repeated reports on the server", async () => {
    let response: Response | undefined;
    for (let index = 0; index < 65; index++) response = await POST(request({ message: "repeated" }));
    expect(response?.status).toBe(429);
  });
  it("rejects a forged Host/Origin pair and cross-site browser requests", async () => {
    const forged = request({ message: "forged" }, "https://other.test");
    forged.headers.set("host", "other.test");
    expect((await POST(forged)).status).toBe(403);
    const foreignRequest = request({ message: "cross-site" });
    foreignRequest.headers.set("sec-fetch-site", "cross-site");
    expect((await POST(foreignRequest)).status).toBe(403);
    expect(error).not.toHaveBeenCalled();
  });
  it("uses the explicitly configured public origin behind a reverse proxy", async () => {
    vi.stubEnv("DATAAGENT_WEB_ORIGIN", "https://public.test");
    expect((await POST(request({ message: "proxy client" }, "https://public.test"))).status).toBe(204);
    expect((await POST(request({ message: "wrong origin" }))).status).toBe(403);
    expect(error).toHaveBeenCalledTimes(1);
  });
  it("uses the server listen origin when Next normalizes the internal request URL", async () => {
    vi.stubEnv("__NEXT_PRIVATE_ORIGIN", "http://127.0.0.1:3000");
    expect((await POST(request({ message: "direct client" }, "http://127.0.0.1:3000"))).status).toBe(204);
    expect((await POST(request({ message: "internal URL origin" }))).status).toBe(403);
  });
  it("cannot reset the process limit by changing forwarded addresses, and resets each minute", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-17T00:00:00Z"));
    for (let index = 0; index < 60; index++) {
      const report = request({ message: "bounded" });
      report.headers.set("x-forwarded-for", `untrusted-${index}`);
      expect((await POST(report)).status).toBe(204);
    }
    const extra = request({ message: "over budget" });
    extra.headers.set("x-forwarded-for", "another-address");
    expect((await POST(extra)).status).toBe(429);
    expect(error).toHaveBeenCalledTimes(60);
    vi.advanceTimersByTime(60_000);
    expect((await POST(request({ message: "next window" }))).status).toBe(204);
  });
  it("limits total body bytes and rechecks the quota after concurrent body reads", async () => {
    const large = { message: "m".repeat(4000), stack: "s".repeat(16000) };
    const size = new TextEncoder().encode(JSON.stringify(large)).byteLength;
    for (let index = 0; index < 60; index++) await POST(request(large));
    expect(error.mock.calls.length * size).toBeLessThanOrEqual(1_048_576);
    expect(error.mock.calls.length).toBeLessThan(60);

    vi.resetModules();
    ({ POST } = await import("./route"));
    error.mockClear();
    const responses = await Promise.all(Array.from({ length: 100 }, (_, index) => {
      const report = request({ message: "concurrent" });
      report.headers.set("x-forwarded-for", `another-${index}`);
      return POST(report);
    }));
    expect(responses.filter(response => response.status === 204)).toHaveLength(60);
    expect(error).toHaveBeenCalledTimes(60);
  });
});
