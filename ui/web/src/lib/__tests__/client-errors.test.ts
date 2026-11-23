// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("client error reporter", () => {
  beforeEach(() => { vi.resetModules(); });
  afterEach(() => { vi.unstubAllGlobals(); });
  it("redacts credentials, preserves run IDs and suppresses duplicate reports", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetch);
    const { reportClientError } = await import("../client-errors");
    const error = new Error("Bearer test-token https://obs.test/a?Signature=signed-token");
    reportClientError(error, { threadId: "thread-1", runId: "run-1" });
    reportClientError(error, { threadId: "thread-1", runId: "run-1" });
    reportClientError(error, { threadId: "thread-1", stage: "client" });
    expect(fetch).toHaveBeenCalledTimes(1);
    const body = fetch.mock.calls[0][1].body;
    expect(body).not.toContain("test-token");
    expect(body).not.toContain("signed-token");
    expect(JSON.parse(body)).toMatchObject({ threadId: "thread-1", runId: "run-1" });
    reportClientError(new Error(error.message), { threadId: "thread-1", runId: "run-2" });
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it("reporting failures cannot throw into the UI", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const { reportClientError } = await import("../client-errors");
    expect(() => reportClientError(new Error("render failure"))).not.toThrow();
    await Promise.resolve();
  });
  it("keeps Unicode reports within the server byte limit and omits unavailable IDs", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetch);
    const { reportClientError } = await import("../client-errors");
    const error = new Error("错误".repeat(2000));
    error.stack = "堆栈".repeat(16000);
    reportClientError(error, JSON.parse('{"threadId":null}'));
    const body = fetch.mock.calls[0][1].body;
    expect(new TextEncoder().encode(body).byteLength).toBeLessThanOrEqual(32_768);
    expect(JSON.parse(body).threadId).toBeUndefined();
    expect(JSON.parse(body).stack).toBeTruthy();
  });
});
